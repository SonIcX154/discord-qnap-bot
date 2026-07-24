from __future__ import annotations

import os
import re
import json
import time
import asyncio
import aiosqlite
from typing import Any, Optional, Callable, Awaitable

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore


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
        # deleted_at für Point-in-Time Restore relativ zu Snapshots
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
