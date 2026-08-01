"""Persistent Discord ↔ Twitch message id map for mirror deletes after restart."""
from __future__ import annotations

import os
import time
import aiosqlite
from typing import Any, Optional


DEFAULT_PATH = os.getenv("TWITCH_MIRROR_DB_PATH", "data/twitch_mirror.db")


class TwitchMapStore:
    """
    Stores bidirectional mappings:
      twitch_id  ↔  discord_id
      direction: 'inbound'  (Twitch → Discord webhook)
                 'outbound' (Discord → Twitch send)
    """

    def __init__(self, path: str = DEFAULT_PATH, max_entries: int = 3000) -> None:
        self.path = path
        self.max_entries = max(100, int(max_entries))

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS msg_map (
                    twitch_id   TEXT PRIMARY KEY,
                    discord_id  INTEGER NOT NULL UNIQUE,
                    login       TEXT,
                    direction   TEXT NOT NULL,
                    created_at  INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_msg_map_discord
                    ON msg_map(discord_id);
                CREATE INDEX IF NOT EXISTS idx_msg_map_login
                    ON msg_map(login);
                CREATE INDEX IF NOT EXISTS idx_msg_map_created
                    ON msg_map(created_at);
                """
            )
            await db.commit()

    async def upsert(
        self,
        *,
        twitch_id: str,
        discord_id: int,
        login: Optional[str],
        direction: str,
    ) -> None:
        if not twitch_id or not discord_id:
            return
        now = int(time.time())
        login_l = (login or "").lower() or None
        async with aiosqlite.connect(self.path) as db:
            # Drop any row that would violate UNIQUE discord_id with a different twitch_id
            await db.execute(
                "DELETE FROM msg_map WHERE discord_id = ? AND twitch_id != ?",
                (discord_id, twitch_id),
            )
            await db.execute(
                """
                INSERT INTO msg_map (twitch_id, discord_id, login, direction, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(twitch_id) DO UPDATE SET
                    discord_id = excluded.discord_id,
                    login = excluded.login,
                    direction = excluded.direction,
                    created_at = excluded.created_at
                """,
                (twitch_id, discord_id, login_l, direction, now),
            )
            await db.commit()
        await self.prune()

    async def delete_by_twitch(self, twitch_id: str) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT twitch_id, discord_id, login, direction FROM msg_map WHERE twitch_id = ?",
                (twitch_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return None
            await db.execute("DELETE FROM msg_map WHERE twitch_id = ?", (twitch_id,))
            await db.commit()
        return {
            "twitch_id": row[0],
            "discord_id": int(row[1]),
            "login": row[2],
            "direction": row[3],
        }

    async def delete_by_discord(
        self,
        discord_id: int,
        *,
        direction: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            if direction:
                async with db.execute(
                    """
                    SELECT twitch_id, discord_id, login, direction
                    FROM msg_map WHERE discord_id = ? AND direction = ?
                    """,
                    (discord_id, direction),
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with db.execute(
                    "SELECT twitch_id, discord_id, login, direction FROM msg_map WHERE discord_id = ?",
                    (discord_id,),
                ) as cur:
                    row = await cur.fetchone()
            if not row:
                return None
            await db.execute(
                "DELETE FROM msg_map WHERE twitch_id = ?", (row[0],)
            )
            await db.commit()
        return {
            "twitch_id": row[0],
            "discord_id": int(row[1]),
            "login": row[2],
            "direction": row[3],
        }

    async def delete_by_login(self, login: str) -> list[dict[str, Any]]:
        login_l = login.lower()
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT twitch_id, discord_id, login, direction FROM msg_map WHERE login = ?",
                (login_l,),
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                return []
            await db.execute("DELETE FROM msg_map WHERE login = ?", (login_l,))
            await db.commit()
        return [
            {
                "twitch_id": r[0],
                "discord_id": int(r[1]),
                "login": r[2],
                "direction": r[3],
            }
            for r in rows
        ]

    async def clear_direction(self, direction: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT twitch_id, discord_id, login, direction FROM msg_map WHERE direction = ?",
                (direction,),
            ) as cur:
                rows = await cur.fetchall()
            if rows:
                await db.execute(
                    "DELETE FROM msg_map WHERE direction = ?", (direction,)
                )
                await db.commit()
        return [
            {
                "twitch_id": r[0],
                "discord_id": int(r[1]),
                "login": r[2],
                "direction": r[3],
            }
            for r in rows
        ]

    async def delete_since(self, since_ts: int) -> list[dict[str, Any]]:
        """Delete all map rows with created_at >= since_ts. Returns deleted rows."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                """
                SELECT twitch_id, discord_id, login, direction, created_at
                FROM msg_map
                WHERE created_at >= ?
                """,
                (int(since_ts),),
            ) as cur:
                rows = await cur.fetchall()
            if rows:
                await db.execute(
                    "DELETE FROM msg_map WHERE created_at >= ?", (int(since_ts),)
                )
                await db.commit()
        return [
            {
                "twitch_id": r[0],
                "discord_id": int(r[1]),
                "login": r[2],
                "direction": r[3],
                "created_at": int(r[4]),
            }
            for r in rows
        ]

    async def load_recent(self) -> list[dict[str, Any]]:
        """Oldest-first list of the most recent max_entries rows."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                """
                SELECT twitch_id, discord_id, login, direction, created_at
                FROM msg_map
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (self.max_entries,),
            ) as cur:
                rows = await cur.fetchall()
        rows = list(reversed(rows))
        return [
            {
                "twitch_id": r[0],
                "discord_id": int(r[1]),
                "login": r[2],
                "direction": r[3],
                "created_at": int(r[4]),
            }
            for r in rows
        ]

    async def count(self) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM msg_map") as cur:
                total = int((await cur.fetchone())[0])
            async with db.execute(
                "SELECT COUNT(*) FROM msg_map WHERE direction = 'inbound'"
            ) as cur:
                inbound = int((await cur.fetchone())[0])
            async with db.execute(
                "SELECT COUNT(*) FROM msg_map WHERE direction = 'outbound'"
            ) as cur:
                outbound = int((await cur.fetchone())[0])
        return {"total": total, "inbound": inbound, "outbound": outbound}

    async def prune(self) -> int:
        """Keep only the newest max_entries rows."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM msg_map") as cur:
                total = int((await cur.fetchone())[0])
            if total <= self.max_entries:
                return 0
            excess = total - self.max_entries
            await db.execute(
                """
                DELETE FROM msg_map WHERE twitch_id IN (
                    SELECT twitch_id FROM msg_map
                    ORDER BY created_at ASC
                    LIMIT ?
                )
                """,
                (excess,),
            )
            await db.commit()
            return excess
