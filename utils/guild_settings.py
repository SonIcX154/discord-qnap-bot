"""Per-guild key/value settings (channels, flags, …) – shared by cogs.

Example keys:
  badgebase.notify_channel   → Discord text channel id
  voice_stayer.channel       → Discord voice channel id
  voice_stayer.enabled       → "1" / "0"

Usage:
  from utils.guild_settings import GuildSettings
  settings = GuildSettings()
  await settings.init()
  await settings.set_channel(guild_id, "badgebase.notify_channel", channel.id)
  cid = await settings.get_channel(guild_id, "badgebase.notify_channel")
"""
from __future__ import annotations

import os
import time
import aiosqlite
from typing import Any, Optional

DEFAULT_PATH = os.getenv("GUILD_SETTINGS_PATH", "data/guild_settings.db")


class GuildSettings:
    """SQLite-backed guild settings. Safe to share one instance across cogs."""

    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = path
        self._ready = False

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id    INTEGER NOT NULL,
                    key         TEXT    NOT NULL,
                    value       TEXT    NOT NULL,
                    updated_at  INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, key)
                );
                CREATE INDEX IF NOT EXISTS idx_guild_settings_key
                    ON guild_settings(key);
                """
            )
            await db.commit()
        self._ready = True

    async def _ensure(self) -> None:
        if not self._ready:
            await self.init()

    async def get(self, guild_id: int, key: str) -> Optional[str]:
        await self._ensure()
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
                (int(guild_id), key),
            ) as cur:
                row = await cur.fetchone()
        return str(row[0]) if row else None

    async def set(self, guild_id: int, key: str, value: str) -> None:
        await self._ensure()
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO guild_settings (guild_id, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (int(guild_id), key, str(value), now),
            )
            await db.commit()

    async def delete(self, guild_id: int, key: str) -> bool:
        await self._ensure()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?",
                (int(guild_id), key),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def get_int(self, guild_id: int, key: str) -> Optional[int]:
        raw = await self.get(guild_id, key)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    async def get_bool(self, guild_id: int, key: str, default: bool = False) -> bool:
        raw = await self.get(guild_id, key)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    async def set_bool(self, guild_id: int, key: str, value: bool) -> None:
        await self.set(guild_id, key, "1" if value else "0")

    async def get_channel(self, guild_id: int, key: str) -> Optional[int]:
        """Return a stored channel id, or None."""
        return await self.get_int(guild_id, key)

    async def set_channel(self, guild_id: int, key: str, channel_id: int) -> None:
        await self.set(guild_id, key, str(int(channel_id)))

    async def clear_channel(self, guild_id: int, key: str) -> bool:
        return await self.delete(guild_id, key)

    async def guilds_with_key(self, key: str) -> list[tuple[int, str]]:
        """All (guild_id, value) pairs that have this key set."""
        await self._ensure()
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT guild_id, value FROM guild_settings WHERE key = ?",
                (key,),
            ) as cur:
                rows = await cur.fetchall()
        return [(int(r[0]), str(r[1])) for r in rows]


# Shared singleton – cogs can import this instead of constructing their own
_shared: Optional[GuildSettings] = None


def get_settings(path: str = DEFAULT_PATH) -> GuildSettings:
    global _shared
    if _shared is None or _shared.path != path:
        _shared = GuildSettings(path)
    return _shared
