from __future__ import annotations

import os
import json
import time
import asyncio
import logging
import aiosqlite
import discord
from datetime import datetime, timezone
from typing import Any, Optional, Callable, Awaitable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

log = logging.getLogger("qnapbot.backup.restore")

MESSAGE_RESTORE_DELAY = 0.75
MAX_RETRIES = 5
PAGE_SIZE = 250


def _display_tz():
    """
    Timezone for human-readable restore timestamps in webhook usernames.

    Prefers BACKUP_DISPLAY_TZ, then Docker/system TZ, then Europe/Berlin.
    Unix timestamps in the DB are always UTC; we only convert for display.
    """
    name = (
        os.getenv("BACKUP_DISPLAY_TZ")
        or os.getenv("TZ")
        or "Europe/Berlin"
    ).strip()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            try:
                return ZoneInfo("Europe/Berlin")
            except Exception:
                pass
    return timezone.utc


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
                log.warning(
                    "429 bei %s – warte %.1fs (Versuch %s/%s)",
                    label, wait, attempt + 1, MAX_RETRIES,
                )
                await asyncio.sleep(wait)
                continue
            raise
        except Exception:
            raise
    assert last_exc is not None
    raise last_exc


def format_username_with_timestamp(author_name: str, created_at: Optional[int]) -> str:
    """Webhook-Username max 80 Zeichen: 'Name • TT.MM.JJJJ HH:MM' (local TZ)."""
    base = (author_name or "Unknown").strip() or "Unknown"
    if not created_at:
        return base[:80]
    try:
        # DB stores unix epoch (UTC); convert to display timezone
        ts = datetime.fromtimestamp(int(created_at), tz=timezone.utc).astimezone(_display_tz())
        stamp = ts.strftime("%d.%m.%Y %H:%M")
        max_name = 80 - len(stamp) - 3
        if max_name < 1:
            return stamp[:80]
        return f"{base[:max_name]} • {stamp}"
    except Exception:
        return base[:80]


def _visibility_sql(as_of: Optional[int], include_deleted: bool) -> tuple[str, list[Any]]:
    params: list[Any] = []
    if as_of is not None:
        clause = (
            "created_at <= ? AND (deleted_at IS NULL OR deleted_at > ?)"
        )
        params.extend([int(as_of), int(as_of)])
        return clause, params
    if include_deleted:
        return "1=1", params
    return "is_deleted = 0", params


async def ensure_deleted_at_column(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        try:
            await db.execute(
                "ALTER TABLE messages ADD COLUMN deleted_at INTEGER"
            )
            await db.commit()
            log.info("messages.deleted_at Spalte hinzugefügt")
        except aiosqlite.OperationalError:
            pass


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
    *,
    as_of: Optional[int] = None,
    include_deleted: bool = True,
) -> int:
    vis, vparams = _visibility_sql(as_of, include_deleted)
    async with aiosqlite.connect(db_path) as db:
        if source_guild_id is None:
            sql = f"SELECT COUNT(*) FROM messages WHERE {vis}"
            params: list[Any] = list(vparams)
        else:
            sql = f"SELECT COUNT(*) FROM messages WHERE guild_id = ? AND {vis}"
            params = [source_guild_id, *vparams]
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def load_channel_ids_with_messages(
    db_path: str,
    source_guild_id: Optional[int] = None,
    *,
    as_of: Optional[int] = None,
    include_deleted: bool = True,
) -> list[int]:
    vis, vparams = _visibility_sql(as_of, include_deleted)
    async with aiosqlite.connect(db_path) as db:
        if source_guild_id is None:
            sql = f"""
                SELECT DISTINCT channel_id FROM messages
                WHERE {vis}
                ORDER BY channel_id
            """
            params: list[Any] = list(vparams)
        else:
            sql = f"""
                SELECT DISTINCT channel_id FROM messages
                WHERE guild_id = ? AND {vis}
                ORDER BY channel_id
            """
            params = [source_guild_id, *vparams]
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


async def load_already_restored_in_guild(
    db_path: str,
    message_ids: list[int],
    guild_channel_ids: set[int],
) -> set[int]:
    if not message_ids or not guild_channel_ids:
        return set()
    restored: set[int] = set()
    async with aiosqlite.connect(db_path) as db:
        try:
            for i in range(0, len(message_ids), 500):
                chunk = message_ids[i : i + 500]
                placeholders = ",".join("?" * len(chunk))
                async with db.execute(
                    f"""
                    SELECT message_id, target_channel_id
                    FROM restored_messages
                    WHERE message_id IN ({placeholders})
                    """,
                    chunk,
                ) as cur:
                    rows = await cur.fetchall()
                    for mid, target_ch in rows:
                        if target_ch is not None and int(target_ch) in guild_channel_ids:
                            restored.add(int(mid))
        except aiosqlite.OperationalError:
            return set()
    return restored


async def mark_restored(db_path: str, message_id: int, target_channel_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        try:
            await db.execute(
                """
                INSERT OR REPLACE INTO restored_messages (message_id, restored_at, target_channel_id)
                VALUES (?, ?, ?)
                """,
                (message_id, int(time.time()), target_channel_id),
            )
            await db.commit()
        except aiosqlite.OperationalError:
            pass


async def load_messages_page(
    db_path: str,
    channel_id: int,
    *,
    after_created_at: Optional[int] = None,
    after_message_id: Optional[int] = None,
    page_size: int = PAGE_SIZE,
    as_of: Optional[int] = None,
    include_deleted: bool = True,
) -> list[dict[str, Any]]:
    vis, vparams = _visibility_sql(as_of, include_deleted)

    if after_created_at is not None and after_message_id is not None:
        query = f"""
            SELECT message_id, author_name, author_avatar, content, embeds, attachments, created_at
            FROM messages
            WHERE channel_id = ? AND {vis}
              AND (created_at > ? OR (created_at = ? AND message_id > ?))
            ORDER BY created_at ASC, message_id ASC
            LIMIT ?
        """
        params: list[Any] = [
            channel_id, *vparams,
            after_created_at, after_created_at, after_message_id,
            page_size,
        ]
    else:
        query = f"""
            SELECT message_id, author_name, author_avatar, content, embeds, attachments, created_at
            FROM messages
            WHERE channel_id = ? AND {vis}
            ORDER BY created_at ASC, message_id ASC
            LIMIT ?
        """
        params = [channel_id, *vparams, page_size]

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
    return result


async def load_messages_for_channel(
    db_path: str,
    channel_id: int,
    *,
    limit: Optional[int],
    guild_channel_ids: Optional[set[int]] = None,
    skip_restored: bool = True,
    as_of: Optional[int] = None,
    include_deleted: bool = True,
) -> list[dict[str, Any]]:
    effective_limit = PAGE_SIZE if limit is None else min(int(limit), PAGE_SIZE * 4)

    vis, vparams = _visibility_sql(as_of, include_deleted)
    query = f"""
        SELECT message_id, author_name, author_avatar, content, embeds, attachments, created_at
        FROM messages
        WHERE channel_id = ? AND {vis}
        ORDER BY created_at ASC
        LIMIT ?
    """
    params: list[Any] = [channel_id, *vparams, effective_limit]

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

    if skip_restored and result and guild_channel_ids is not None:
        already = await load_already_restored_in_guild(
            db_path, [m["message_id"] for m in result], guild_channel_ids
        )
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
        log.error("Webhook erstellen fehlgeschlagen in #%s: %s", channel.name, e)
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

            username = format_username_with_timestamp(
                msg.get("author_name") or "Unknown",
                msg.get("created_at"),
            )
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
                log.warning(
                    "Webhook-Send fehlgeschlagen (msg %s): %s",
                    msg.get("message_id"), e,
                )

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
    force: bool = False,
    as_of: Optional[int] = None,
    include_deleted: bool = True,
) -> None:
    await ensure_deleted_at_column(db_path)

    name_lookup = build_name_lookup_from_snapshot(snapshot_data)
    id_map = await load_channel_id_map(db_path, guild.id)
    guild_channel_ids = {c.id for c in guild.channels}

    mode = f"as_of={as_of}" if as_of else f"include_deleted={include_deleted}"
    log.info(
        "Message-Restore start: guild=%s id_map=%s name_lookup=%s "
        "source_guild_id=%s force=%s %s display_tz=%s",
        guild.id, len(id_map), len(name_lookup),
        source_guild_id, force, mode,
        os.getenv("BACKUP_DISPLAY_TZ") or os.getenv("TZ") or "Europe/Berlin",
    )

    if channel_filter is not None:
        all_ids = await load_channel_ids_with_messages(
            db_path, source_guild_id, as_of=as_of, include_deleted=include_deleted
        )
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
        old_ids = await load_channel_ids_with_messages(
            db_path, source_guild_id, as_of=as_of, include_deleted=include_deleted
        )

    log.info("Channels mit Nachrichten in DB: %s", len(old_ids))

    total_sent = 0
    total_errors = 0
    total_skipped = 0
    total_empty = 0
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
        if channels_skipped or total_skipped or total_empty:
            embed.add_field(
                name="Übersprungen",
                value=(
                    f"Channels ohne Ziel: {channels_skipped}\n"
                    f"Leer: {total_empty} · bereits restored: {total_skipped}"
                ),
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
                log.debug(
                    "Kein Ziel-Channel für old_id=%s (name=%r)",
                    oid, name_lookup.get(oid),
                )
                continue

            if not target.permissions_for(guild.me).manage_webhooks:
                channels_skipped += 1
                log.warning("Keine Manage Webhooks Permission in #%s", target.name)
                continue

            await update(f"Channel **#{target.name}** …")

            channel_sent = 0
            channel_empty = 0
            channel_errors = 0
            channel_skipped = 0
            remaining = limit_per_channel

            after_created_at: Optional[int] = None
            after_message_id: Optional[int] = None

            while True:
                if remaining is not None and remaining <= 0:
                    break

                page_limit = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
                raw_page = await load_messages_page(
                    db_path,
                    oid,
                    after_created_at=after_created_at,
                    after_message_id=after_message_id,
                    page_size=page_limit,
                    as_of=as_of,
                    include_deleted=include_deleted,
                )

                if not raw_page:
                    break

                last_raw = raw_page[-1]
                after_created_at = (
                    int(last_raw["created_at"])
                    if last_raw.get("created_at") is not None
                    else None
                )
                after_message_id = int(last_raw["message_id"])

                to_send = raw_page
                if not force and guild_channel_ids:
                    already = await load_already_restored_in_guild(
                        db_path,
                        [m["message_id"] for m in raw_page],
                        guild_channel_ids,
                    )
                    to_send = [m for m in raw_page if m["message_id"] not in already]
                    channel_skipped += len(raw_page) - len(to_send)

                if to_send:
                    sent, empty, errors = await restore_messages_to_channel(
                        target, to_send, db_path=db_path
                    )
                    channel_sent += sent
                    channel_empty += empty
                    channel_errors += errors

                if remaining is not None:
                    remaining -= len(raw_page)

                if len(raw_page) < page_limit:
                    break

            total_sent += channel_sent
            total_empty += channel_empty
            total_errors += channel_errors
            total_skipped += channel_skipped
            channels_done += 1
            log.info(
                "#%s: %s sent, %s empty, %s err, %s already-restored",
                target.name, channel_sent, channel_empty, channel_errors, channel_skipped,
            )

        as_of_txt = (
            datetime.fromtimestamp(as_of, tz=timezone.utc)
            .astimezone(_display_tz())
            .strftime("%Y-%m-%d %H:%M %Z")
            if as_of else "alle (inkl. gelöschte)"
        )
        embed = discord.Embed(
            title="✅ Nachrichten-Restore abgeschlossen",
            color=discord.Color.green() if total_sent else discord.Color.orange(),
        )
        embed.add_field(name="Gesendet", value=f"**{total_sent:,}**", inline=True)
        embed.add_field(name="Channels", value=f"**{channels_done}**", inline=True)
        embed.add_field(name="Fehler", value=f"**{total_errors}**", inline=True)
        embed.add_field(
            name="Details",
            value=(
                f"Zeitpunkt: **{as_of_txt}**\n"
                f"Ohne Ziel-Channel: **{channels_skipped}**\n"
                f"Leer (kein Text/Embed/File): **{total_empty}**\n"
                f"Bereits restored (dieser Server): **{total_skipped}**\n"
                f"id_map: **{len(id_map)}** · name_lookup: **{len(name_lookup)}**"
            ),
            inline=False,
        )
        if total_sent == 0:
            embed.set_footer(
                text="0 gesendet – prüfe Logs. Tipp: Struktur-Restore zuerst."
            )
        else:
            embed.set_footer(text="Webhook · Avatar/Name · Timestamp (local TZ) · batched")
        try:
            await progress_msg.edit(embed=embed, view=None)
        except Exception:
            pass

    except Exception as e:
        log.exception("Nachrichten-Restore abgebrochen: %s", e)
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
