from __future__ import annotations

import os
import json
import asyncio
import aiosqlite
import discord
from typing import Any, Optional

MESSAGE_RESTORE_DELAY = 0.75  # Pause zwischen Webhook-Sends (Rate-Limits)


async def resolve_target_channel(
    guild: discord.Guild,
    old_channel_id: int,
    *,
    match_by_name: bool,
    name_lookup: dict[int, str],
) -> Optional[discord.TextChannel]:
    """Findet den Ziel-Channel: zuerst per ID, optional per Name."""
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
    """old_channel_id -> name aus einem Struktur-Snapshot."""
    if not snapshot_data:
        return {}
    lookup: dict[int, str] = {}
    for ch in snapshot_data.get("channels", []):
        if ch.get("id") and ch.get("name"):
            lookup[int(ch["id"])] = ch["name"]
    return lookup


async def load_channel_ids_with_messages(db_path: str, guild_id: int) -> list[int]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """
            SELECT DISTINCT channel_id FROM messages
            WHERE guild_id = ? AND is_deleted = 0
            ORDER BY channel_id
            """,
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


async def load_messages_for_channel(
    db_path: str,
    channel_id: int,
    *,
    limit: Optional[int],
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
) -> tuple[int, int]:
    """
    Spielt Nachrichten per Webhook in den Channel ein.
    Returns (sent_count, error_count).
    """
    if not messages:
        return 0, 0

    try:
        webhook = await channel.create_webhook(
            name="Backup Restore",
            reason="Nachrichten-Restore aus Backup",
        )
    except Exception as e:
        print(f"[Backup] Webhook erstellen fehlgeschlagen in #{channel.name}: {e}")
        return 0, 1

    sent = 0
    errors = 0

    try:
        for msg in messages:
            content = (msg.get("content") or "")[:2000]
            embeds = _parse_embeds(msg.get("embeds"))
            files = _build_files(msg.get("attachments"))

            # Leere Nachricht ohne Inhalt/Embeds/Files überspringen
            if not content and not embeds and not files:
                continue

            username = (msg.get("author_name") or "Unknown")[:80]
            avatar_url = msg.get("author_avatar") or None

            try:
                await webhook.send(
                    content=content or None,
                    username=username,
                    avatar_url=avatar_url,
                    embeds=embeds or discord.utils.MISSING,
                    files=files or discord.utils.MISSING,
                    wait=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent += 1
            except Exception as e:
                errors += 1
                print(f"[Backup] Webhook-Send fehlgeschlagen (msg {msg.get('message_id')}): {e}")

            await asyncio.sleep(MESSAGE_RESTORE_DELAY)
    finally:
        try:
            await webhook.delete(reason="Backup Restore fertig")
        except Exception:
            pass

    return sent, errors


async def run_message_restore(
    *,
    guild: discord.Guild,
    db_path: str,
    progress_msg: discord.WebhookMessage | discord.Message,
    channel_filter: Optional[discord.TextChannel] = None,
    limit_per_channel: Optional[int] = None,
    match_by_name: bool = True,
    snapshot_data: Optional[dict[str, Any]] = None,
) -> None:
    """Hauptablauf: Nachrichten für den gesamten Server oder einen Channel restoren."""
    name_lookup = build_name_lookup_from_snapshot(snapshot_data)

    if channel_filter is not None:
        # Nur dieser Channel – old id = current id (gleiche ID)
        old_ids = [channel_filter.id]
        # Wenn match_by_name und wir Nachrichten unter anderer ID haben:
        # zusätzlich alle DB-Channels prüfen die denselben Namen haben
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT DISTINCT channel_id FROM messages WHERE guild_id = ? AND is_deleted = 0",
                (guild.id,),
            ) as cur:
                all_ids = [int(r[0]) for r in await cur.fetchall()]
        # Finde DB-Channel-IDs die auf channel_filter mappen
        mapped: list[int] = []
        for oid in all_ids:
            target = await resolve_target_channel(
                guild, oid, match_by_name=match_by_name, name_lookup=name_lookup
            )
            if target and target.id == channel_filter.id:
                mapped.append(oid)
        if not mapped:
            mapped = [channel_filter.id]
        old_ids = mapped
    else:
        old_ids = await load_channel_ids_with_messages(db_path, guild.id)

    total_sent = 0
    total_errors = 0
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
        if channels_skipped:
            embed.add_field(name="Übersprungen", value=str(channels_skipped), inline=True)
        try:
            await progress_msg.edit(embed=embed, view=None)
        except Exception:
            pass

    try:
        for oid in old_ids:
            target = await resolve_target_channel(
                guild, oid, match_by_name=match_by_name, name_lookup=name_lookup
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
                db_path, oid, limit=limit_per_channel
            )
            sent, errors = await restore_messages_to_channel(target, messages)
            total_sent += sent
            total_errors += errors
            channels_done += 1
            print(f"[Backup] #{target.name}: {sent} Nachrichten restored, {errors} Fehler")

        embed = discord.Embed(
            title="✅ Nachrichten-Restore abgeschlossen",
            color=discord.Color.green(),
        )
        embed.add_field(name="Gesendet", value=f"**{total_sent:,}**", inline=True)
        embed.add_field(name="Channels", value=f"**{channels_done}**", inline=True)
        embed.add_field(name="Fehler", value=f"**{total_errors}**", inline=True)
        if channels_skipped:
            embed.add_field(name="Übersprungen", value=str(channels_skipped), inline=True)
        embed.set_footer(
            text="Via Webhook · Name/Avatar original · Timestamps sind neu · Mentions deaktiviert"
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
