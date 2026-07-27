from __future__ import annotations

import os
import re
import json
import time
import shutil
import asyncio
import aiosqlite
from typing import Any, Optional, Callable, Awaitable

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore

# Soft-deleted rows older than this are hard-deleted by the auto task
SOFT_DELETE_RETENTION_DAYS = int(os.getenv("BACKUP_SOFT_DELETE_RETENTION_DAYS", "30"))
PURGE_BATCH_SIZE = 500


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(" .")
    return name[:200] if name else "file"


async def ensure_extra_tables(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS channel_id_map (
                guild_id        INTEGER NOT NULL,
                old_channel_id  INTEGER NOT NULL,
                new_channel_id  INTEGER NOT NULL,
                updated_at      INTEGER,
                PRIMARY KEY (guild_id, old_channel_id)
            );

            CREATE TABLE IF NOT EXISTS restored_messages (
                message_id        INTEGER PRIMARY KEY,
                restored_at       INTEGER NOT NULL,
                target_channel_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS excluded_channels (
                channel_id  INTEGER PRIMARY KEY,
                guild_id    INTEGER NOT NULL,
                reason      TEXT,
                created_at  INTEGER
            );

            CREATE TABLE IF NOT EXISTS excluded_guilds (
                guild_id    INTEGER PRIMARY KEY,
                reason      TEXT,
                created_at  INTEGER
            );
        """)
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN deleted_at INTEGER")
            print("[Backup] messages.deleted_at Spalte hinzugefügt")
        except aiosqlite.OperationalError:
            pass
        await db.commit()


async def save_channel_id_map(db_path: str, guild_id: int, mapping: dict[int, int]) -> None:
    if not mapping:
        return
    now = int(time.time())
    async with aiosqlite.connect(db_path) as db:
        for old_id, new_id in mapping.items():
            await db.execute(
                """
                INSERT INTO channel_id_map (guild_id, old_channel_id, new_channel_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, old_channel_id) DO UPDATE SET
                    new_channel_id = excluded.new_channel_id,
                    updated_at = excluded.updated_at
                """,
                (guild_id, old_id, new_id, now),
            )
        await db.commit()


async def is_channel_excluded(db_path: str, channel_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        try:
            async with db.execute(
                "SELECT 1 FROM excluded_channels WHERE channel_id = ?",
                (channel_id,),
            ) as cur:
                return await cur.fetchone() is not None
        except aiosqlite.OperationalError:
            return False


async def is_guild_excluded(db_path: str, guild_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        try:
            async with db.execute(
                "SELECT 1 FROM excluded_guilds WHERE guild_id = ?",
                (guild_id,),
            ) as cur:
                return await cur.fetchone() is not None
        except aiosqlite.OperationalError:
            return False


async def add_excluded_guild(
    db_path: str, guild_id: int, reason: Optional[str] = None
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO excluded_guilds (guild_id, reason, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET reason = excluded.reason
            """,
            (guild_id, reason, int(time.time())),
        )
        await db.commit()


async def remove_excluded_guild(db_path: str, guild_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "DELETE FROM excluded_guilds WHERE guild_id = ?", (guild_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def list_excluded_guilds(db_path: str) -> list[tuple[int, Optional[str]]]:
    async with aiosqlite.connect(db_path) as db:
        try:
            async with db.execute(
                "SELECT guild_id, reason FROM excluded_guilds"
            ) as cur:
                rows = await cur.fetchall()
        except aiosqlite.OperationalError:
            return []
    return [(int(r[0]), r[1]) for r in rows]


async def add_excluded_channel(
    db_path: str, channel_id: int, guild_id: int, reason: Optional[str] = None
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO excluded_channels (channel_id, guild_id, reason, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                reason = excluded.reason
            """,
            (channel_id, guild_id, reason, int(time.time())),
        )
        await db.commit()


async def remove_excluded_channel(db_path: str, channel_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "DELETE FROM excluded_channels WHERE channel_id = ?", (channel_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def list_excluded_channels(db_path: str, guild_id: int) -> list[tuple[int, Optional[str]]]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT channel_id, reason FROM excluded_channels WHERE guild_id = ?",
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def _remove_attachment_dir(attachments_dir: str, message_id: int) -> bool:
    """Delete data/backups/attachments/<message_id>/ if present."""
    path = os.path.join(attachments_dir, str(message_id))
    if not os.path.isdir(path):
        return False
    try:
        shutil.rmtree(path, ignore_errors=False)
        return True
    except Exception as e:
        print(f"[Backup] Attachment-Dir löschen fehlgeschlagen ({message_id}): {e}")
        return False


async def _fetch_message_ids(
    db: aiosqlite.Connection,
    where_sql: str,
    params: list[Any],
    limit: int = PURGE_BATCH_SIZE,
) -> list[int]:
    async with db.execute(
        f"SELECT message_id FROM messages WHERE {where_sql} LIMIT ?",
        [*params, limit],
    ) as cur:
        rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


async def _hard_delete_message_ids(
    db_path: str,
    attachments_dir: str,
    message_ids: list[int],
) -> dict[str, int]:
    """Hard-delete rows + attachment folders + restored_messages entries."""
    if not message_ids:
        return {"rows": 0, "attachments": 0, "restored": 0}

    att_removed = 0
    for mid in message_ids:
        if _remove_attachment_dir(attachments_dir, mid):
            att_removed += 1

    placeholders = ",".join("?" * len(message_ids))
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            f"DELETE FROM messages WHERE message_id IN ({placeholders})",
            message_ids,
        )
        rows = cur.rowcount or 0
        try:
            cur2 = await db.execute(
                f"DELETE FROM restored_messages WHERE message_id IN ({placeholders})",
                message_ids,
            )
            restored = cur2.rowcount or 0
        except aiosqlite.OperationalError:
            restored = 0
        await db.commit()

    return {"rows": rows, "attachments": att_removed, "restored": restored}


async def backfill_missing_deleted_at(db_path: str) -> int:
    """
    Soft-deleted rows without deleted_at get deleted_at = now so the
    30-day retention clock starts from the first purge pass (not created_at).
    """
    now = int(time.time())
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            UPDATE messages
            SET deleted_at = ?
            WHERE is_deleted = 1 AND deleted_at IS NULL
            """,
            (now,),
        )
        await db.commit()
        return cur.rowcount or 0


async def purge_soft_deleted(
    db_path: str,
    attachments_dir: str,
    *,
    older_than_days: int = SOFT_DELETE_RETENTION_DAYS,
) -> dict[str, int]:
    """
    Hard-delete messages marked is_deleted=1 whose deleted_at is older than N days.
    Also removes matching attachment folders.
    """
    await ensure_extra_tables(db_path)
    backfilled = await backfill_missing_deleted_at(db_path)

    cutoff = int(time.time()) - max(0, int(older_than_days)) * 86400
    where = "is_deleted = 1 AND deleted_at IS NOT NULL AND deleted_at < ?"
    params: list[Any] = [cutoff]

    totals = {"rows": 0, "attachments": 0, "restored": 0, "backfilled_deleted_at": backfilled}

    while True:
        async with aiosqlite.connect(db_path) as db:
            ids = await _fetch_message_ids(db, where, params)
        if not ids:
            break
        stats = await _hard_delete_message_ids(db_path, attachments_dir, ids)
        totals["rows"] += stats["rows"]
        totals["attachments"] += stats["attachments"]
        totals["restored"] += stats["restored"]
        if len(ids) < PURGE_BATCH_SIZE:
            break
        await asyncio.sleep(0.05)

    return totals


async def purge_all_soft_deleted(
    db_path: str,
    attachments_dir: str,
) -> dict[str, int]:
    """Hard-delete every is_deleted=1 row (manual prune command)."""
    await ensure_extra_tables(db_path)
    where = "is_deleted = 1"
    params: list[Any] = []
    totals = {"rows": 0, "attachments": 0, "restored": 0}

    while True:
        async with aiosqlite.connect(db_path) as db:
            ids = await _fetch_message_ids(db, where, params)
        if not ids:
            break
        stats = await _hard_delete_message_ids(db_path, attachments_dir, ids)
        totals["rows"] += stats["rows"]
        totals["attachments"] += stats["attachments"]
        totals["restored"] += stats["restored"]
        if len(ids) < PURGE_BATCH_SIZE:
            break
        await asyncio.sleep(0.05)

    return totals


async def purge_excluded_guild_messages(
    db_path: str,
    attachments_dir: str,
) -> dict[str, int]:
    """Hard-delete all messages belonging to guilds in excluded_guilds."""
    await ensure_extra_tables(db_path)
    excluded = await list_excluded_guilds(db_path)
    if not excluded:
        return {"rows": 0, "attachments": 0, "restored": 0, "guilds": 0}

    guild_ids = [g for g, _ in excluded]
    placeholders = ",".join("?" * len(guild_ids))
    where = f"guild_id IN ({placeholders})"
    params: list[Any] = list(guild_ids)
    totals = {"rows": 0, "attachments": 0, "restored": 0, "guilds": len(guild_ids)}

    while True:
        async with aiosqlite.connect(db_path) as db:
            ids = await _fetch_message_ids(db, where, params)
        if not ids:
            break
        stats = await _hard_delete_message_ids(db_path, attachments_dir, ids)
        totals["rows"] += stats["rows"]
        totals["attachments"] += stats["attachments"]
        totals["restored"] += stats["restored"]
        if len(ids) < PURGE_BATCH_SIZE:
            break
        await asyncio.sleep(0.05)

    return totals


async def count_purge_candidates(db_path: str) -> dict[str, int]:
    """Stats for the purge command preview."""
    await ensure_extra_tables(db_path)
    cutoff = int(time.time()) - SOFT_DELETE_RETENTION_DAYS * 86400
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE is_deleted = 1"
        ) as cur:
            soft = int((await cur.fetchone())[0])
        async with db.execute(
            """
            SELECT COUNT(*) FROM messages
            WHERE is_deleted = 1 AND deleted_at IS NOT NULL AND deleted_at < ?
            """,
            (cutoff,),
        ) as cur:
            soft_expired = int((await cur.fetchone())[0])
        try:
            async with db.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE guild_id IN (SELECT guild_id FROM excluded_guilds)
                """
            ) as cur:
                excluded = int((await cur.fetchone())[0])
        except aiosqlite.OperationalError:
            excluded = 0
        async with db.execute("SELECT COUNT(*) FROM messages") as cur:
            total = int((await cur.fetchone())[0])
    return {
        "soft_deleted": soft,
        "soft_deleted_expired": soft_expired,
        "excluded_guild": excluded,
        "total": total,
        "retention_days": SOFT_DELETE_RETENTION_DAYS,
    }


async def _download_url(url: str, path: str) -> bool:
    if aiohttp is None:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 429:
                    retry = float(resp.headers.get("Retry-After", "3"))
                    await asyncio.sleep(retry)
                    async with session.get(url) as resp2:
                        if resp2.status != 200:
                            return False
                        data = await resp2.read()
                elif resp.status != 200:
                    return False
                else:
                    data = await resp.read()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except Exception as e:
        print(f"[Backup] URL-Download fehlgeschlagen: {e}")
        return False


async def download_missing_attachments(
    db_path: str,
    attachments_dir: str,
    *,
    guild_id: Optional[int] = None,
    limit: int = 500,
    on_progress: Optional[Callable[[int, int, int], Awaitable[None]]] = None,
) -> dict[str, int]:
    query = "SELECT message_id, attachments FROM messages WHERE attachments IS NOT NULL AND is_deleted = 0"
    params: list[Any] = []
    if guild_id is not None:
        query += " AND guild_id = ?"
        params.append(guild_id)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()

    stats = {"checked": 0, "downloaded": 0, "failed": 0, "already_ok": 0, "updated_rows": 0}
    processed = 0

    for message_id, attachments_json in rows:
        if processed >= limit:
            break
        try:
            items = json.loads(attachments_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(items, list) or not items:
            continue

        changed = False
        msg_dir = os.path.join(attachments_dir, str(message_id))

        for item in items:
            stats["checked"] += 1
            path = item.get("local_path")
            if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                stats["already_ok"] += 1
                continue

            url = item.get("url")
            filename = _safe_filename(item.get("filename") or "file")
            local_path = os.path.join(msg_dir, filename)

            if os.path.isfile(local_path) and os.path.getsize(local_path) > 0:
                item["local_path"] = local_path
                changed = True
                stats["already_ok"] += 1
                continue

            if not url:
                stats["failed"] += 1
                continue

            ok = await _download_url(url, local_path)
            if ok:
                item["local_path"] = local_path
                changed = True
                stats["downloaded"] += 1
            else:
                stats["failed"] += 1

            await asyncio.sleep(0.35)

        if changed:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "UPDATE messages SET attachments = ? WHERE message_id = ?",
                    (json.dumps(items, ensure_ascii=False), message_id),
                )
                await db.commit()
            stats["updated_rows"] += 1

        processed += 1
        if on_progress and processed % 10 == 0:
            await on_progress(processed, stats["downloaded"], stats["failed"])

    return stats
