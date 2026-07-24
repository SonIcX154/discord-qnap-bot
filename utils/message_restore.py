from __future__ import annotations

import os
import json
import time
import asyncio
import aiosqlite
import discord
from typing import Any, Optional, Callable, Awaitable

MESSAGE_RESTORE_DELAY = 0.75
MAX_RETRIES = 5


async def with_retry(coro_factory: Callable[[], Awaitable[Any]], *,
                     label: str = "api") -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            return await coro_factory()
        except discord.HTTPException as e:
            last_exc = e
            if e.status == 429:
                retry_after = getattr(e, "retry_after", None)
                if retry_after is None:
                    try:
                        retry_after = float(e.response.headers.get("Retry-After", 2))  # type: ignore[union-attr]
                    except Exception:
                        retry_after = 2.0 * (attempt + 1)
                wait = max(float(retry_after), 0.5)
                print(f"[Backup] 429 bei {label} – warte {wait:.1f}s (Versuch {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
                continue
            raise
        except Exception:
            raise
    assert last_exc is not None
    raise last_exc


async def load_channel_id_map(db_path: str, guild_id: int) -> dict[int, int]:
    async with aiosqlite.connect(db_path) as db:
        try:
            async with db.execute(
                "SELECT old_channel_id, new_channel_id FROM channel_id_map WHERE guild_id = ?",
                (guild_id,),
            ) as cur:
                rows = await cur.fetchall()
        except aiosqlite.OperationalError:
            return {}
    return {int(r[0]): int(r[1]) for r in rows}


async def resolve_target_channel(
    guild: discord.Guild,
    old_channel_id: int,
    *,
    match_by_name: bool,
    name_lookup: dict[int, str],
    id_map: dict[int, int],
) -> Optional[discord.TextChannel]:
    if old_channel_id in id_map:
        ch = guild.get_channel(id_map[old_channel_id])
        if isinstance(ch, discord.TextChannel):
            return ch

    ch = guild.get_channel(old_channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch

    if not match_by_name:
        return None

    name = name_lookup.get(old_channel_id)
    if not name:
        return None

    for c in guild.text_channels:
        if c.name == name:
            return c
    return None


def build_name_lookup_from_snapshot(snapshot_data: Optional[dict[str, Any]]) -> dict[int, str]:
    if not snapshot_data:
        return {}
    lookup: dict[int, str] = {}
    for ch in snapshot_data.get("channels", []):
        if ch.get("id") and ch.get("name"):
            lookup[int(ch["id"])] = ch["name"]
    return lookup


async def count_messages(
    db_path: str,
    source_guild_id: Optional[int] = None,
) -> int:
    """Anzahl gespeicherter Nachrichten. source_guild_id=None → ganze Instanz."""
    async with aiosqlite.connect(db_path) as db:
        if source_guild_id is None:
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE is_deleted = 0"
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND is_deleted = 0",
                (source_guild_id,),
            ) as cur:
                row = await cur.fetchone()
    return int(row[0]) if row else 0


async def load_channel_ids_with_messages(
    db_path: str,
    source_guild_id: Optional[int] = None,
) -> list[int]:
    """Channel-IDs mit Nachrichten. None = instanz-weit (Disaster Recovery)."""
    async with aiosqlite.connect(db_path) as db:
        if source_guild_id is None:
            async with db.execute(
                """
                SELECT DISTINCT channel_id FROM messages
                WHERE is_deleted = 0
                ORDER BY channel_id
                """
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                """
                SELECT DISTINCT channel_id FROM messages
                WHERE guild_id = ? AND is_deleted = 0
                ORDER BY channel_id
                """,
                (source_guild_id,),
            ) as cur:
                rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


async def load_already_restored(db_path: str, message_ids: list[int]) -> set[int]:
    if not message_ids:
        return set()
    restored: set[int] = set()
    async with aiosqlite.connect(db_path) as db:
        try:
            for i in range(0, len(message_ids), 500):
                chunk = message_ids[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                async with db.execute(
                    f"SELECT message_id FROM restored_messages WHERE message_id IN ({placeholders})",
                    chunk,
                ) as cur:
                    rows = await cur.fetchall()
                    restored.update(int(r[0]) for r in rows)
        except aiosqlite.OperationalError:
            return set()
    return restored


async def mark_restored(db_path: str, message_id: int, target_channel_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        try:
            await db.execute(
                """
                INSERT OR IGNORE INTO restored_messages (message_id, restored_at, target_channel_id)
                VALUES (?, ?, ?)
                """,
                (message_id, int(time.time()), target_channel_id),
            )
            await db.commit()
        except aiosqlite.OperationalError:
            pass


async def load_messages_for_channel(
    db_path: str,
    channel_id: int,
    *,
    limit: Optional[int],
    skip_restored: bool = True,
) -> list[dict[str, Any]]:
    query = """
        SELECT message_id, author_name, author_avatar, content, embeds, attachments, created_at
        FROM messages
        WHERE channel_id = ? AND is_deleted = 0
        ORDER BY created_at ASC
    """
    params: list[Any] = [channel_id]
    if limit is not None and limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        result.append({
            "message_id": row[0],
            "author_name": row[1] or "Unknown",
            "author_avatar": row[2],
            "content": row[3] or "",
            "embeds": row[4],
            "attachments": row[5],
            "created_at": row[6],
        })

    if skip_restored and result:
        already = await load_already_restored(db_path, [m["message_id"] for m in result])
        result = [m for m in result if m["message_id"] not in already]

    return result


def _parse_embeds(embeds_json: Optional[str]) -> list[discord.Embed]:
    if not embeds_json:
        return []
    try:
        raw = json.loads(embeds_json)
    except json.JSONDecodeError:
        return []
    embeds: list[discord.Embed] = []
    for item in raw[:10]:
        try:
            embeds.append(discord.Embed.from_dict(item))
        except Exception:
            continue
    return embeds


def _build_files(attachments_json: Optional[str]) -> list[discord.File]:
    if not attachments_json:
        return []
    try:
        items = json.loads(attachments_json)
    except json.JSONDecodeError:
        return []

    files: list[discord.File] = []
    for item in items[:10]:
        path = item.get("local_path")
        filename = item.get("filename") or "file"
        if path and os.path.isfile(path):
            try:
                files.append(discord.File(path, filename=filename))
            except Exception:
                continue
    return files


async def restore_messages_to_channel(
    channel: discord.TextChannel,
    messages: list[dict[str, Any]],
    *,
    db_path: str,
) -> tuple[int, int, int]:
    if not messages:
        return 0, 0, 0

    try:
        webhook = await with_retry(
            lambda: channel.create_webhook(
                name="Backup Restore",
                reason="Nachrichten-Restore aus Backup",
            ),
            label=f"create_webhook #{channel.name}",
        )
    except Exception as e:
        print(f"[Backup] Webhook erstellen fehlgeschlagen in #{channel.name}: {e}")
        return 0, 0, 1

    sent = 0
    skipped = 0
    errors = 0

    try:
        for msg in messages:
            content = (msg.get("content") or "")[:2000]
            embeds = _parse_embeds(msg.get("embeds"))
            files = _build_files(msg.get("attachments"))

            if not content and not embeds and not files:
                skipped += 1
                continue

            username = (msg.get("author_name") or "Unknown")[:80]
            avatar_url = msg.get("author_avatar") or None

            try:
                await with_retry(
                    lambda c=content, u=username, a=avatar_url, e=embeds, f=files: webhook.send(
                        content=c or None,
                        username=u,
                        avatar_url=a,
                        embeds=e or discord.utils.MISSING,
                        files=f or discord.utils.MISSING,
                        wait=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    ),
                    label=f"webhook.send #{channel.name}",
                )
                sent += 1
                await mark_restored(db_path, int(msg["message_id"]), channel.id)
            except Exception as e:
                errors += 1
                print(f"[Backup] Webhook-Send fehlgeschlagen (msg {msg.get('message_id')}): {e}")

            await asyncio.sleep(MESSAGE_RESTORE_DELAY)
    finally:
        try:
            await with_retry(
                lambda: webhook.delete(reason="Backup Restore fertig"),
                label="webhook.delete",
            )
        except Exception:
            pass

    return sent, skipped, errors


async def run_message_restore(
    *,
    guild: discord.Guild,
    db_path: str,
    progress_msg: discord.WebhookMessage | discord.Message,
    channel_filter: Optional[discord.TextChannel] = None,
    limit_per_channel: Optional[int] = None,
    match_by_name: bool = True,
    snapshot_data: Optional[dict[str, Any]] = None,
    source_guild_id: Optional[int] = None,
) -> None:
    """
    source_guild_id: Alte Guild-ID aus dem Snapshot.
    None = alle Nachrichten der Instanz (eine Instanz = ein logischer Server).
    """
    if source_guild_id is None and snapshot_data:
        source_guild_id = (snapshot_data.get("guild") or {}).get("id")
        if source_guild_id is not None:
            source_guild_id = int(source_guild_id)

    name_lookup = build_name_lookup_from_snapshot(snapshot_data)
    id_map = await load_channel_id_map(db_path, guild.id)

    if channel_filter is not None:
        all_ids = await load_channel_ids_with_messages(db_path, source_guild_id)
        mapped: list[int] = []
        for oid in all_ids:
            target = await resolve_target_channel(
                guild, oid,
                match_by_name=match_by_name,
                name_lookup=name_lookup,
                id_map=id_map,
            )
            if target and target.id == channel_filter.id:
                mapped.append(oid)
        old_ids = mapped or [channel_filter.id]
    else:
        old_ids = await load_channel_ids_with_messages(db_path, source_guild_id)

    total_sent = 0
    total_errors = 0
    total_skipped = 0
    channels_done = 0
    channels_skipped = 0

    async def update(step: str) -> None:
        embed = discord.Embed(
            title="🔄 Nachrichten-Restore läuft...",
            description=step,
            color=discord.Color.orange(),
        )
        embed.add_field(name="Gesendet", value=f"**{total_sent:,}**", inline=True)
        embed.add_field(name="Channels", value=f"**{channels_done}** / {len(old_ids)}", inline=True)
        embed.add_field(name="Fehler", value=f"**{total_errors}**", inline=True)
        if channels_skipped or total_skipped:
            embed.add_field(
                name="Übersprungen",
                value=f"Channels: {channels_skipped} · leer/bereits: {total_skipped}",
                inline=False,
            )
        try:
            await progress_msg.edit(embed=embed, view=None)
        except Exception:
            pass

    try:
        for oid in old_ids:
            target = await resolve_target_channel(
                guild, oid,
                match_by_name=match_by_name,
                name_lookup=name_lookup,
                id_map=id_map,
            )
            if target is None:
                channels_skipped += 1
                print(f"[Backup] Kein Ziel-Channel für old_id={oid}")
                continue

            if not target.permissions_for(guild.me).manage_webhooks:
                channels_skipped += 1
                print(f"[Backup] Keine Manage Webhooks Permission in #{target.name}")
                continue

            await update(f"Channel **#{target.name}** …")

            messages = await load_messages_for_channel(
                db_path, oid, limit=limit_per_channel, skip_restored=True
            )
            sent, skipped, errors = await restore_messages_to_channel(
                target, messages, db_path=db_path
            )
            total_sent += sent
            total_skipped += skipped
            total_errors += errors
            channels_done += 1
            print(f"[Backup] #{target.name}: {sent} sent, {skipped} skip, {errors} err")

        embed = discord.Embed(
            title="✅ Nachrichten-Restore abgeschlossen",
            color=discord.Color.green(),
        )
        embed.add_field(name="Gesendet", value=f"**{total_sent:,}**", inline=True)
        embed.add_field(name="Channels", value=f"**{channels_done}**", inline=True)
        embed.add_field(name="Fehler", value=f"**{total_errors}**", inline=True)
        embed.set_footer(
            text="Webhook · Avatar/Name aus Backup · bereits restored werden übersprungen"
        )
        try:
            await progress_msg.edit(embed=embed, view=None)
        except Exception:
            pass

    except Exception as e:
        print(f"[Backup] Nachrichten-Restore abgebrochen: {e}")
        try:
            await progress_msg.edit(
                embed=discord.Embed(
                    title="❌ Nachrichten-Restore fehlgeschlagen",
                    description=f"`{e}`",
                    color=discord.Color.red(),
                ),
                view=None,
            )
        except Exception:
            pass
