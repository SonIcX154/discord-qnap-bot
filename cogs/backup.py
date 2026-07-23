from __future__ import annotations

import os
import re
import json
import time
import asyncio
import aiosqlite
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Any
from datetime import datetime, timezone

try:
    from utils.message_restore import run_message_restore
except ImportError:
    from ..utils.message_restore import run_message_restore

BACKUP_DB_PATH = os.getenv("BACKUP_DATA_PATH", "data/backup.db")
ATTACHMENTS_DIR = os.getenv("BACKUP_ATTACHMENTS_PATH", "data/backups/attachments")

BACKFILL_BATCH_SIZE = 100
BACKFILL_DELAY = 1.1
RESTORE_DELAY = 0.4


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(" .")
    return name[:200] if name else "file"


class RestoreConfirmView(discord.ui.View):
    """Bestätigung vor dem Struktur-Restore."""

    def __init__(self, cog: "BackupCog", snapshot_id: int, clear_first: bool) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.snapshot_id = snapshot_id
        self.clear_first = clear_first
        self.confirmed = False

    @discord.ui.button(label="✅ Wiederherstellen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            content="Restore abgebrochen.",
            embed=None,
            view=None,
        )
        self.stop()


class MessageRestoreConfirmView(discord.ui.View):
    """Bestätigung vor dem Nachrichten-Restore."""

    def __init__(self) -> None:
        super().__init__(timeout=60)
        self.confirmed = False

    @discord.ui.button(label="✅ Nachrichten wiederherstellen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            content="Nachrichten-Restore abgebrochen.",
            embed=None,
            view=None,
        )
        self.stop()


class BackupCog(commands.Cog):
    """Server Backup System – Logging, Backfill, Attachments, Snapshots + Restore."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = BACKUP_DB_PATH
        self._catchup_lock = asyncio.Lock()
        self._backfill_task: Optional[asyncio.Task] = None
        self._restore_task: Optional[asyncio.Task] = None
        self._msg_restore_task: Optional[asyncio.Task] = None
        self._backfill_status: dict[str, Any] = {
            "running": False,
            "current_channel": None,
            "channels_done": 0,
            "channels_total": 0,
            "messages_this_run": 0,
        }

    async def cog_load(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
        await self._init_db()
        print(f"[Backup] Cog loaded. DB: {self.db_path}")
        self.bot.loop.create_task(self._wait_and_catchup())

    async def cog_unload(self) -> None:
        for task in (self._backfill_task, self._restore_task, self._msg_restore_task):
            if task and not task.done():
                task.cancel()

    async def _init_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS channel_progress (
                    channel_id      INTEGER PRIMARY KEY,
                    guild_id        INTEGER NOT NULL,
                    last_message_id INTEGER,
                    oldest_message_id INTEGER,
                    fully_backfilled INTEGER DEFAULT 0,
                    updated_at      INTEGER
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id      INTEGER PRIMARY KEY,
                    channel_id      INTEGER NOT NULL,
                    guild_id        INTEGER NOT NULL,
                    author_id       INTEGER,
                    author_name     TEXT,
                    author_avatar   TEXT,
                    content         TEXT,
                    embeds          TEXT,
                    attachments     TEXT,
                    reference_id    INTEGER,
                    created_at      INTEGER NOT NULL,
                    edited_at       INTEGER,
                    is_deleted      INTEGER DEFAULT 0,
                    raw_data        TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_messages_channel_created
                    ON messages(channel_id, created_at);

                CREATE TABLE IF NOT EXISTS snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id        INTEGER NOT NULL,
                    name            TEXT,
                    created_at      INTEGER NOT NULL,
                    created_by      INTEGER,
                    data            TEXT NOT NULL
                );
            """)
            await db.commit()

    # ==================== ATTACHMENTS ====================

    async def _download_attachments(self, message: discord.Message) -> list[dict[str, Any]]:
        if not message.attachments:
            return []

        result: list[dict[str, Any]] = []
        msg_dir = os.path.join(ATTACHMENTS_DIR, str(message.id))
        os.makedirs(msg_dir, exist_ok=True)

        for att in message.attachments:
            entry: dict[str, Any] = {
                "filename": att.filename,
                "url": att.url,
                "size": att.size,
                "content_type": att.content_type,
                "local_path": None,
            }

            safe_name = _safe_filename(att.filename)
            local_path = os.path.join(msg_dir, safe_name)

            if os.path.isfile(local_path) and os.path.getsize(local_path) > 0:
                entry["local_path"] = local_path
                result.append(entry)
                continue

            try:
                await att.save(local_path)
                entry["local_path"] = local_path
            except Exception as e:
                print(f"[Backup] Attachment-Download fehlgeschlagen ({message.id}/{att.filename}): {e}")

            result.append(entry)

        return result

    # ==================== MESSAGE LOGGING ====================

    async def _store_message(self, message: discord.Message, *, is_edit: bool = False) -> None:
        if not message.guild:
            return

        if self.bot.user and message.author.id == self.bot.user.id:
            return

        embeds_json = (
            json.dumps([e.to_dict() for e in message.embeds], ensure_ascii=False)
            if message.embeds else None
        )

        attachments_data = await self._download_attachments(message)
        attachments_json = (
            json.dumps(attachments_data, ensure_ascii=False) if attachments_data else None
        )

        created_at = int(message.created_at.timestamp())
        edited_at = int(message.edited_at.timestamp()) if message.edited_at else None
        reference_id = message.reference.message_id if message.reference else None

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO messages (
                    message_id, channel_id, guild_id, author_id, author_name, author_avatar,
                    content, embeds, attachments, reference_id, created_at, edited_at, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(message_id) DO UPDATE SET
                    content = excluded.content,
                    embeds = excluded.embeds,
                    attachments = excluded.attachments,
                    edited_at = excluded.edited_at
            """, (
                message.id,
                message.channel.id,
                message.guild.id,
                message.author.id,
                message.author.display_name,
                str(message.author.display_avatar.url),
                message.content,
                embeds_json,
                attachments_json,
                reference_id,
                created_at,
                edited_at
            ))

            await db.execute("""
                INSERT INTO channel_progress (
                    channel_id, guild_id, last_message_id, oldest_message_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    last_message_id = MAX(COALESCE(last_message_id, 0), excluded.last_message_id),
                    oldest_message_id = CASE
                        WHEN oldest_message_id IS NULL THEN excluded.oldest_message_id
                        ELSE MIN(oldest_message_id, excluded.oldest_message_id)
                    END,
                    updated_at = excluded.updated_at
            """, (
                message.channel.id,
                message.guild.id,
                message.id,
                message.id,
                int(time.time())
            ))

            await db.commit()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._store_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        await self._store_message(after, is_edit=True)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE messages SET is_deleted = 1 WHERE message_id = ?",
                (payload.message_id,)
            )
            await db.commit()

    # ==================== CATCH-UP ====================

    async def _wait_and_catchup(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)
        await self._catchup_all_guilds()

    async def _catchup_all_guilds(self) -> None:
        async with self._catchup_lock:
            for guild in self.bot.guilds:
                print(f"[Backup] Catch-up für {guild.name}...")
                for channel in guild.text_channels:
                    if not channel.permissions_for(guild.me).read_message_history:
                        continue
                    try:
                        await self._catchup_channel(channel)
                    except Exception as e:
                        print(f"[Backup] Catch-up Fehler in #{channel.name}: {e}")

    async def _catchup_channel(self, channel: discord.TextChannel) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT last_message_id FROM channel_progress WHERE channel_id = ?",
                (channel.id,)
            ) as cursor:
                row = await cursor.fetchone()
                last_id = row[0] if row else None

        if last_id is None:
            history = channel.history(limit=50, oldest_first=True)
        else:
            history = channel.history(
                after=discord.Object(id=last_id),
                oldest_first=True,
                limit=None,
            )

        count = 0
        async for message in history:
            await self._store_message(message)
            count += 1
            if count % 100 == 0:
                await asyncio.sleep(0.5)

        if count:
            print(f"[Backup] #{channel.name}: {count} Nachrichten nachgeholt")

    # ==================== BACKFILL ====================

    async def _backfill_channel(self, channel: discord.TextChannel) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT oldest_message_id, fully_backfilled FROM channel_progress WHERE channel_id = ?",
                (channel.id,)
            ) as cursor:
                row = await cursor.fetchone()

        if row and row[1] == 1:
            return 0

        oldest_id: Optional[int] = row[0] if row else None
        total_saved = 0

        while True:
            kwargs: dict[str, Any] = {
                "limit": BACKFILL_BATCH_SIZE,
                "oldest_first": False,
            }
            if oldest_id is not None:
                kwargs["before"] = discord.Object(id=oldest_id)

            batch = [msg async for msg in channel.history(**kwargs)]

            if not batch:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute("""
                        INSERT INTO channel_progress (channel_id, guild_id, fully_backfilled, updated_at)
                        VALUES (?, ?, 1, ?)
                        ON CONFLICT(channel_id) DO UPDATE SET
                            fully_backfilled = 1,
                            updated_at = excluded.updated_at
                    """, (channel.id, channel.guild.id, int(time.time())))
                    await db.commit()
                break

            for message in batch:
                await self._store_message(message)
                total_saved += 1

            oldest_id = batch[-1].id

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO channel_progress (
                        channel_id, guild_id, oldest_message_id, updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        oldest_message_id = excluded.oldest_message_id,
                        updated_at = excluded.updated_at
                """, (channel.id, channel.guild.id, oldest_id, int(time.time())))
                await db.commit()

            self._backfill_status["messages_this_run"] += len(batch)
            await asyncio.sleep(BACKFILL_DELAY)

        return total_saved

    async def _run_backfill(self, guild: discord.Guild, progress_msg: discord.WebhookMessage | discord.Message) -> None:
        self._backfill_status = {
            "running": True,
            "current_channel": None,
            "channels_done": 0,
            "channels_total": 0,
            "messages_this_run": 0,
        }

        channels = [
            c for c in guild.text_channels
            if c.permissions_for(guild.me).read_message_history
        ]
        self._backfill_status["channels_total"] = len(channels)

        try:
            for i, channel in enumerate(channels, start=1):
                self._backfill_status["current_channel"] = channel.name
                self._backfill_status["channels_done"] = i - 1

                embed = discord.Embed(
                    title="📦 Historischer Backfill läuft...",
                    color=discord.Color.orange(),
                )
                embed.add_field(name="Aktueller Channel", value=f"#{channel.name}", inline=False)
                embed.add_field(name="Fortschritt", value=f"{i-1} / {len(channels)} Channels", inline=True)
                embed.add_field(
                    name="Nachrichten diese Runde",
                    value=f"{self._backfill_status['messages_this_run']:,}",
                    inline=True,
                )
                embed.set_footer(text="Läuft im Hintergrund")

                try:
                    await progress_msg.edit(embed=embed)
                except Exception:
                    pass

                try:
                    saved = await self._backfill_channel(channel)
                    print(f"[Backup] Backfill #{channel.name}: +{saved} Nachrichten")
                except Exception as e:
                    print(f"[Backup] Backfill Fehler in #{channel.name}: {e}")

                self._backfill_status["channels_done"] = i

            embed = discord.Embed(
                title="✅ Historischer Backfill abgeschlossen",
                color=discord.Color.green(),
            )
            embed.add_field(name="Channels", value=f"{len(channels)}", inline=True)
            embed.add_field(
                name="Neue Nachrichten",
                value=f"{self._backfill_status['messages_this_run']:,}",
                inline=True,
            )
            try:
                await progress_msg.edit(embed=embed)
            except Exception:
                pass

        finally:
            self._backfill_status["running"] = False
            self._backfill_status["current_channel"] = None
            self._backfill_task = None

    # ==================== STRUKTUR-SNAPSHOT ====================

    def _serialize_overwrite(self, ow: discord.PermissionOverwrite, target_id: int, target_type: str) -> dict[str, Any]:
        allow, deny = ow.pair()
        return {
            "id": target_id,
            "type": target_type,
            "allow": allow.value,
            "deny": deny.value,
        }

    def _serialize_channel(self, channel: discord.abc.GuildChannel) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": channel.id,
            "name": channel.name,
            "type": channel.type.value if hasattr(channel.type, "value") else int(channel.type),
            "position": channel.position,
            "category_id": channel.category_id,
            "overwrites": [],
        }

        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Role):
                data["overwrites"].append(
                    self._serialize_overwrite(overwrite, target.id, "role")
                )
            elif isinstance(target, discord.Member):
                data["overwrites"].append(
                    self._serialize_overwrite(overwrite, target.id, "member")
                )

        if isinstance(channel, discord.TextChannel):
            data.update({
                "topic": channel.topic,
                "nsfw": channel.nsfw,
                "rate_limit_per_user": channel.slowmode_delay,
                "default_auto_archive_duration": getattr(channel, "default_auto_archive_duration", None),
            })
        elif isinstance(channel, discord.VoiceChannel):
            data.update({
                "bitrate": channel.bitrate,
                "user_limit": channel.user_limit,
                "rtc_region": str(channel.rtc_region) if channel.rtc_region else None,
            })
        elif isinstance(channel, discord.StageChannel):
            data.update({
                "bitrate": channel.bitrate,
                "user_limit": channel.user_limit,
                "rtc_region": str(channel.rtc_region) if channel.rtc_region else None,
                "topic": channel.topic,
            })
        elif isinstance(channel, discord.ForumChannel):
            data.update({
                "topic": channel.topic,
                "nsfw": channel.nsfw,
                "rate_limit_per_user": channel.slowmode_delay,
                "default_auto_archive_duration": getattr(channel, "default_auto_archive_duration", None),
            })

        return data

    def _serialize_role(self, role: discord.Role) -> dict[str, Any]:
        return {
            "id": role.id,
            "name": role.name,
            "color": role.color.value,
            "permissions": role.permissions.value,
            "position": role.position,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "managed": role.managed,
            "display_icon": str(role.display_icon) if role.display_icon else None,
        }

    def _build_structure_snapshot(self, guild: discord.Guild) -> dict[str, Any]:
        roles = [
            self._serialize_role(role)
            for role in sorted(guild.roles, key=lambda r: r.position)
            if role != guild.default_role
        ]

        everyone = self._serialize_role(guild.default_role)

        categories = [
            self._serialize_channel(cat)
            for cat in sorted(guild.categories, key=lambda c: c.position)
        ]

        channels = [
            self._serialize_channel(ch)
            for ch in sorted(guild.channels, key=lambda c: c.position)
            if not isinstance(ch, discord.CategoryChannel)
        ]

        guild_settings = {
            "id": guild.id,
            "name": guild.name,
            "description": guild.description,
            "icon_url": str(guild.icon.url) if guild.icon else None,
            "banner_url": str(guild.banner.url) if guild.banner else None,
            "afk_channel_id": guild.afk_channel.id if guild.afk_channel else None,
            "afk_timeout": guild.afk_timeout,
            "verification_level": guild.verification_level.value,
            "explicit_content_filter": guild.explicit_content_filter.value,
            "default_notifications": guild.default_notifications.value,
            "system_channel_id": guild.system_channel.id if guild.system_channel else None,
            "rules_channel_id": guild.rules_channel.id if guild.rules_channel else None,
            "public_updates_channel_id": (
                guild.public_updates_channel.id if guild.public_updates_channel else None
            ),
            "preferred_locale": str(guild.preferred_locale) if guild.preferred_locale else None,
        }

        return {
            "version": 1,
            "created_at": int(time.time()),
            "guild": guild_settings,
            "everyone_role": everyone,
            "roles": roles,
            "categories": categories,
            "channels": channels,
        }

    async def _save_snapshot(
        self,
        guild: discord.Guild,
        name: Optional[str],
        created_by: int,
    ) -> int:
        data = self._build_structure_snapshot(guild)
        data_json = json.dumps(data, ensure_ascii=False)

        if not name:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            name = f"Snapshot {ts}"

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO snapshots (guild_id, name, created_at, created_by, data)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild.id, name, int(time.time()), created_by, data_json),
            )
            await db.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    async def _load_snapshot(self, snapshot_id: int, guild_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT name, data FROM snapshots WHERE id = ? AND guild_id = ?",
                (snapshot_id, guild_id),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return {"name": row[0], "data": json.loads(row[1])}

    async def _load_latest_snapshot_data(self, guild_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT data FROM snapshots
                WHERE guild_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (guild_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return json.loads(row[0])

    # ==================== STRUCTURE RESTORE ====================

    def _build_overwrites(
        self,
        guild: discord.Guild,
        overwrites_data: list[dict[str, Any]],
        role_map: dict[int, int],
    ) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        result: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}

        for ow in overwrites_data:
            target: Optional[discord.abc.Snowflake] = None
            if ow["type"] == "role":
                old_id = ow["id"]
                if old_id == guild.id or role_map.get(old_id) == guild.default_role.id:
                    target = guild.default_role
                else:
                    new_role_id = role_map.get(old_id)
                    if new_role_id:
                        target = guild.get_role(new_role_id)
            elif ow["type"] == "member":
                member = guild.get_member(ow["id"])
                if member:
                    target = member

            if target is None:
                continue

            result[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(ow["allow"]),
                discord.Permissions(ow["deny"]),
            )

        return result

    async def _clear_guild_structure(self, guild: discord.Guild) -> tuple[int, int]:
        deleted_channels = 0
        deleted_roles = 0

        for channel in list(guild.channels):
            try:
                await channel.delete(reason="Backup Restore: clear_first")
                deleted_channels += 1
                await asyncio.sleep(RESTORE_DELAY)
            except Exception as e:
                print(f"[Backup] Channel-Löschen fehlgeschlagen ({channel.name}): {e}")

        me = guild.me
        bot_top = me.top_role.position if me else 0

        for role in sorted(list(guild.roles), key=lambda r: r.position, reverse=True):
            if role.is_default() or role.managed:
                continue
            if role.position >= bot_top:
                continue
            try:
                await role.delete(reason="Backup Restore: clear_first")
                deleted_roles += 1
                await asyncio.sleep(RESTORE_DELAY)
            except Exception as e:
                print(f"[Backup] Rollen-Löschen fehlgeschlagen ({role.name}): {e}")

        return deleted_channels, deleted_roles

    async def _restore_structure(
        self,
        guild: discord.Guild,
        data: dict[str, Any],
        progress_msg: discord.WebhookMessage | discord.Message,
        clear_first: bool,
    ) -> None:
        role_map: dict[int, int] = {}
        old_guild_id = data.get("guild", {}).get("id")
        if old_guild_id:
            role_map[old_guild_id] = guild.default_role.id
        everyone_data = data.get("everyone_role") or {}
        if everyone_data.get("id"):
            role_map[everyone_data["id"]] = guild.default_role.id

        channel_map: dict[int, int] = {}
        stats = {"roles": 0, "categories": 0, "channels": 0, "errors": 0}

        async def update_progress(step: str) -> None:
            embed = discord.Embed(
                title="🔄 Struktur-Restore läuft...",
                description=step,
                color=discord.Color.orange(),
            )
            embed.add_field(name="Rollen", value=str(stats["roles"]), inline=True)
            embed.add_field(name="Kategorien", value=str(stats["categories"]), inline=True)
            embed.add_field(name="Channels", value=str(stats["channels"]), inline=True)
            if stats["errors"]:
                embed.add_field(name="Fehler", value=str(stats["errors"]), inline=True)
            try:
                await progress_msg.edit(embed=embed, view=None)
            except Exception:
                pass

        try:
            if clear_first:
                await update_progress("Lösche bestehende Struktur...")
                dc, dr = await self._clear_guild_structure(guild)
                print(f"[Backup] Clear: {dc} Channels, {dr} Rollen gelöscht")

            await update_progress("Aktualisiere @everyone...")
            if everyone_data.get("permissions") is not None:
                try:
                    await guild.default_role.edit(
                        permissions=discord.Permissions(everyone_data["permissions"]),
                        reason="Backup Restore",
                    )
                except Exception as e:
                    print(f"[Backup] @everyone edit fehlgeschlagen: {e}")
                    stats["errors"] += 1

            await update_progress("Erstelle Rollen...")
            roles_sorted = sorted(
                [r for r in data.get("roles", []) if not r.get("managed")],
                key=lambda r: r.get("position", 0),
            )

            for role_data in roles_sorted:
                try:
                    new_role = await guild.create_role(
                        name=role_data["name"],
                        permissions=discord.Permissions(role_data.get("permissions", 0)),
                        colour=discord.Colour(role_data.get("color", 0)),
                        hoist=role_data.get("hoist", False),
                        mentionable=role_data.get("mentionable", False),
                        reason="Backup Restore",
                    )
                    role_map[role_data["id"]] = new_role.id
                    stats["roles"] += 1
                    await asyncio.sleep(RESTORE_DELAY)
                except Exception as e:
                    print(f"[Backup] Rolle '{role_data.get('name')}' fehlgeschlagen: {e}")
                    stats["errors"] += 1

            for role_data in sorted(roles_sorted, key=lambda r: r.get("position", 0), reverse=True):
                new_id = role_map.get(role_data["id"])
                if not new_id:
                    continue
                role = guild.get_role(new_id)
                if not role:
                    continue
                try:
                    pos = min(role_data.get("position", 1), guild.me.top_role.position - 1) if guild.me else 1
                    if pos < 1:
                        pos = 1
                    await role.edit(position=pos, reason="Backup Restore positions")
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

            await update_progress("Erstelle Kategorien...")
            for cat_data in sorted(data.get("categories", []), key=lambda c: c.get("position", 0)):
                try:
                    overwrites = self._build_overwrites(guild, cat_data.get("overwrites", []), role_map)
                    new_cat = await guild.create_category(
                        name=cat_data["name"],
                        overwrites=overwrites or None,
                        reason="Backup Restore",
                    )
                    channel_map[cat_data["id"]] = new_cat.id
                    stats["categories"] += 1
                    await asyncio.sleep(RESTORE_DELAY)
                except Exception as e:
                    print(f"[Backup] Kategorie '{cat_data.get('name')}' fehlgeschlagen: {e}")
                    stats["errors"] += 1

            await update_progress("Erstelle Channels...")
            for ch_data in sorted(data.get("channels", []), key=lambda c: c.get("position", 0)):
                try:
                    overwrites = self._build_overwrites(guild, ch_data.get("overwrites", []), role_map)
                    parent = None
                    if ch_data.get("category_id") and ch_data["category_id"] in channel_map:
                        parent = guild.get_channel(channel_map[ch_data["category_id"]])

                    ch_type = ch_data.get("type", 0)
                    name = ch_data["name"]

                    if ch_type in (0, 5):
                        new_ch = await guild.create_text_channel(
                            name=name,
                            topic=ch_data.get("topic"),
                            nsfw=ch_data.get("nsfw", False),
                            slowmode_delay=ch_data.get("rate_limit_per_user") or 0,
                            category=parent,  # type: ignore[arg-type]
                            overwrites=overwrites or None,
                            reason="Backup Restore",
                        )
                    elif ch_type == 2:
                        new_ch = await guild.create_voice_channel(
                            name=name,
                            bitrate=min(ch_data.get("bitrate") or 64000, guild.bitrate_limit),
                            user_limit=ch_data.get("user_limit") or 0,
                            category=parent,  # type: ignore[arg-type]
                            overwrites=overwrites or None,
                            reason="Backup Restore",
                        )
                    elif ch_type == 13:
                        new_ch = await guild.create_stage_channel(
                            name=name,
                            topic=ch_data.get("topic"),
                            category=parent,  # type: ignore[arg-type]
                            overwrites=overwrites or None,
                            reason="Backup Restore",
                        )
                    elif ch_type == 15:
                        new_ch = await guild.create_forum(
                            name=name,
                            topic=ch_data.get("topic"),
                            nsfw=ch_data.get("nsfw", False),
                            slowmode_delay=ch_data.get("rate_limit_per_user") or 0,
                            category=parent,  # type: ignore[arg-type]
                            overwrites=overwrites or None,
                            reason="Backup Restore",
                        )
                    else:
                        new_ch = await guild.create_text_channel(
                            name=name,
                            category=parent,  # type: ignore[arg-type]
                            overwrites=overwrites or None,
                            reason="Backup Restore",
                        )

                    channel_map[ch_data["id"]] = new_ch.id
                    stats["channels"] += 1
                    await asyncio.sleep(RESTORE_DELAY)
                except Exception as e:
                    print(f"[Backup] Channel '{ch_data.get('name')}' fehlgeschlagen: {e}")
                    stats["errors"] += 1

            await update_progress("Aktualisiere Server-Settings...")
            g = data.get("guild") or {}
            try:
                kwargs: dict[str, Any] = {}
                if g.get("afk_timeout") is not None:
                    kwargs["afk_timeout"] = g["afk_timeout"]
                if g.get("verification_level") is not None:
                    kwargs["verification_level"] = discord.VerificationLevel(g["verification_level"])
                if g.get("explicit_content_filter") is not None:
                    kwargs["explicit_content_filter"] = discord.ContentFilter(g["explicit_content_filter"])
                if g.get("default_notifications") is not None:
                    kwargs["default_notifications"] = discord.NotificationLevel(g["default_notifications"])

                if g.get("afk_channel_id") and g["afk_channel_id"] in channel_map:
                    kwargs["afk_channel"] = guild.get_channel(channel_map[g["afk_channel_id"]])
                if g.get("system_channel_id") and g["system_channel_id"] in channel_map:
                    kwargs["system_channel"] = guild.get_channel(channel_map[g["system_channel_id"]])

                if kwargs:
                    await guild.edit(**kwargs, reason="Backup Restore")
            except Exception as e:
                print(f"[Backup] Guild-Settings fehlgeschlagen: {e}")
                stats["errors"] += 1

            embed = discord.Embed(
                title="✅ Struktur-Restore abgeschlossen",
                color=discord.Color.green(),
            )
            embed.add_field(name="Rollen", value=f"**{stats['roles']}**", inline=True)
            embed.add_field(name="Kategorien", value=f"**{stats['categories']}**", inline=True)
            embed.add_field(name="Channels", value=f"**{stats['channels']}**", inline=True)
            if stats["errors"]:
                embed.add_field(name="Fehler", value=f"**{stats['errors']}**", inline=True)
                embed.set_footer(text="Details stehen in den Bot-Logs")
            else:
                embed.set_footer(text="Managed Rollen (Bots/Integrationen) wurden übersprungen")

            try:
                await progress_msg.edit(embed=embed, view=None)
            except Exception:
                pass

        except Exception as e:
            print(f"[Backup] Restore abgebrochen: {e}")
            try:
                await progress_msg.edit(
                    embed=discord.Embed(
                        title="❌ Restore fehlgeschlagen",
                        description=f"`{e}`",
                        color=discord.Color.red(),
                    ),
                    view=None,
                )
            except Exception:
                pass
        finally:
            self._restore_task = None

    async def _run_message_restore_task(self, **kwargs: Any) -> None:
        try:
            await run_message_restore(**kwargs)
        finally:
            self._msg_restore_task = None

    # ==================== COMMANDS ====================

    @app_commands.command(name="backup-status", description="Zeigt den aktuellen Backup-Status an")
    @app_commands.default_permissions(administrator=True)
    async def backup_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM messages WHERE is_deleted = 0") as cur:
                total_msgs = (await cur.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM channel_progress") as cur:
                tracked_channels = (await cur.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM channel_progress WHERE fully_backfilled = 1"
            ) as cur:
                fully_done = (await cur.fetchone())[0]

            if guild_id:
                async with db.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE guild_id = ?",
                    (guild_id,),
                ) as cur:
                    snapshot_count = (await cur.fetchone())[0]
            else:
                snapshot_count = 0

        attachment_count = 0
        attachment_size = 0
        if os.path.isdir(ATTACHMENTS_DIR):
            for root, _, files in os.walk(ATTACHMENTS_DIR):
                for f in files:
                    attachment_count += 1
                    try:
                        attachment_size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass

        size_mb = attachment_size / (1024 * 1024)

        embed = discord.Embed(title="📦 Backup Status", color=discord.Color.blurple())
        embed.add_field(name="Gespeicherte Nachrichten", value=f"**{total_msgs:,}**", inline=True)
        embed.add_field(name="Überwachte Channels", value=f"**{tracked_channels}**", inline=True)
        embed.add_field(name="Vollständig backfilled", value=f"**{fully_done}**", inline=True)
        embed.add_field(
            name="Attachments",
            value=f"**{attachment_count:,}** Dateien ({size_mb:.1f} MB)",
            inline=True,
        )
        embed.add_field(name="Struktur-Snapshots", value=f"**{snapshot_count}**", inline=True)

        if self._backfill_status["running"]:
            embed.add_field(
                name="🔄 Backfill läuft",
                value=(
                    f"Channel: **#{self._backfill_status['current_channel']}**\n"
                    f"Fortschritt: {self._backfill_status['channels_done']} / {self._backfill_status['channels_total']}\n"
                    f"Nachrichten diese Runde: {self._backfill_status['messages_this_run']:,}"
                ),
                inline=False,
            )
            embed.color = discord.Color.orange()
        elif self._restore_task and not self._restore_task.done():
            embed.add_field(name="🔄 Struktur-Restore", value="läuft…", inline=False)
            embed.color = discord.Color.orange()
        elif self._msg_restore_task and not self._msg_restore_task.done():
            embed.add_field(name="🔄 Nachrichten-Restore", value="läuft…", inline=False)
            embed.color = discord.Color.orange()
        else:
            embed.set_footer(text="Logging · Attachments · Snapshots · Structure/Message Restore")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="backup-backfill", description="Startet den historischen Backfill aller Channels")
    @app_commands.default_permissions(administrator=True)
    async def backup_backfill(self, interaction: discord.Interaction) -> None:
        if self._backfill_task and not self._backfill_task.done():
            await interaction.response.send_message(
                "⚠️ Ein Backfill läuft bereits.",
                ephemeral=True,
            )
            return

        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        embed = discord.Embed(
            title="📦 Historischer Backfill wird gestartet...",
            description=(
                "Der Bot holt jetzt die gesamte Nachrichten-History aller Text-Channels.\n"
                "Attachments werden dabei ebenfalls heruntergeladen."
            ),
            color=discord.Color.orange(),
        )
        progress_msg = await interaction.followup.send(embed=embed)

        self._backfill_task = asyncio.create_task(
            self._run_backfill(interaction.guild, progress_msg)
        )

    @app_commands.command(
        name="backup-snapshot",
        description="Erstellt einen Struktur-Snapshot (Rollen, Channels, Permissions)",
    )
    @app_commands.describe(name="Optionaler Name für den Snapshot")
    @app_commands.default_permissions(administrator=True)
    async def backup_snapshot(
        self,
        interaction: discord.Interaction,
        name: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            snapshot_id = await self._save_snapshot(
                interaction.guild,
                name=name,
                created_by=interaction.user.id,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Snapshot fehlgeschlagen: `{e}`", ephemeral=True)
            return

        data = self._build_structure_snapshot(interaction.guild)
        display_name = name or f"Snapshot #{snapshot_id}"

        embed = discord.Embed(title="✅ Struktur-Snapshot erstellt", color=discord.Color.green())
        embed.add_field(name="ID", value=f"**#{snapshot_id}**", inline=True)
        embed.add_field(name="Name", value=display_name, inline=True)
        embed.add_field(name="Rollen", value=f"**{len(data['roles']) + 1}**", inline=True)
        embed.add_field(name="Kategorien", value=f"**{len(data['categories'])}**", inline=True)
        embed.add_field(name="Channels", value=f"**{len(data['channels'])}**", inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="backup-snapshots",
        description="Listet alle Struktur-Snapshots dieses Servers",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_snapshots(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT id, name, created_at
                FROM snapshots
                WHERE guild_id = ?
                ORDER BY created_at DESC
                LIMIT 25
                """,
                (interaction.guild.id,),
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await interaction.followup.send(
                "Noch keine Snapshots. Nutze `/backup-snapshot`.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📦 Struktur-Snapshots – {interaction.guild.name}",
            color=discord.Color.blurple(),
        )
        lines = []
        for snap_id, snap_name, created_at in rows:
            ts = datetime.fromtimestamp(created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"**#{snap_id}** · {snap_name or 'Unbenannt'} · `{ts}`")
        embed.description = "\n".join(lines)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="backup-restore",
        description="Stellt die Server-Struktur aus einem Snapshot wieder her",
    )
    @app_commands.describe(
        snapshot_id="ID des Snapshots",
        clear_first="Vorher Channels und (nicht-managed) Rollen löschen",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_restore(
        self,
        interaction: discord.Interaction,
        snapshot_id: int,
        clear_first: bool = False,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
            return

        if self._restore_task and not self._restore_task.done():
            await interaction.response.send_message("⚠️ Ein Restore läuft bereits.", ephemeral=True)
            return

        snap = await self._load_snapshot(snapshot_id, interaction.guild.id)
        if not snap:
            await interaction.response.send_message(
                f"❌ Snapshot **#{snapshot_id}** nicht gefunden.",
                ephemeral=True,
            )
            return

        data = snap["data"]
        role_count = len([r for r in data.get("roles", []) if not r.get("managed")])
        cat_count = len(data.get("categories", []))
        ch_count = len(data.get("channels", []))

        warning = ""
        if clear_first:
            warning = "\n\n⚠️ **clear_first**: Bestehende Channels/Rollen werden gelöscht."

        embed = discord.Embed(
            title="⚠️ Struktur-Restore bestätigen",
            description=(
                f"Snapshot **#{snapshot_id}** – {snap['name'] or 'Unbenannt'}\n\n"
                f"Rollen: **{role_count}** · Kategorien: **{cat_count}** · Channels: **{ch_count}**"
                f"{warning}"
            ),
            color=discord.Color.orange(),
        )

        view = RestoreConfirmView(self, snapshot_id, clear_first)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            return

        progress_msg = await interaction.followup.send(
            embed=discord.Embed(title="🔄 Struktur-Restore startet...", color=discord.Color.orange()),
            ephemeral=False,
        )

        self._restore_task = asyncio.create_task(
            self._restore_structure(interaction.guild, data, progress_msg, clear_first)
        )

    @app_commands.command(
        name="backup-restore-messages",
        description="Stellt gespeicherte Nachrichten per Webhook wieder her",
    )
    @app_commands.describe(
        channel="Nur diesen Channel restoren (sonst alle)",
        limit="Max. Nachrichten pro Channel (leer = alle)",
        match_by_name="Channel per Name matchen, falls ID fehlt (nach Struktur-Restore)",
        snapshot_id="Optional: Snapshot für Namens-Mapping (sonst neuester)",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_restore_messages(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        limit: Optional[app_commands.Range[int, 1, 10000]] = None,
        match_by_name: bool = True,
        snapshot_id: Optional[int] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
            return

        if self._msg_restore_task and not self._msg_restore_task.done():
            await interaction.response.send_message(
                "⚠️ Ein Nachrichten-Restore läuft bereits.",
                ephemeral=True,
            )
            return

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND is_deleted = 0",
                (interaction.guild.id,),
            ) as cur:
                total = (await cur.fetchone())[0]

        if total == 0:
            await interaction.response.send_message(
                "Keine gespeicherten Nachrichten für diesen Server.",
                ephemeral=True,
            )
            return

        snapshot_data: Optional[dict[str, Any]] = None
        if snapshot_id is not None:
            snap = await self._load_snapshot(snapshot_id, interaction.guild.id)
            if snap:
                snapshot_data = snap["data"]
        else:
            snapshot_data = await self._load_latest_snapshot_data(interaction.guild.id)

        scope = f"nur **#{channel.name}**" if channel else "**alle Channels**"
        limit_txt = f"max. **{limit}**/Channel" if limit else "**alle** Nachrichten"

        embed = discord.Embed(
            title="⚠️ Nachrichten-Restore bestätigen",
            description=(
                f"Gespeicherte Nachrichten: **{total:,}**\n"
                f"Ziel: {scope}\n"
                f"Limit: {limit_txt}\n"
                f"Name-Match: **{'an' if match_by_name else 'aus'}**\n\n"
                "Nachrichten werden per **Webhook** eingefügt "
                "(Original-Name + Avatar, lokale Attachments).\n"
                "Timestamps sind neu · Mentions sind deaktiviert."
            ),
            color=discord.Color.orange(),
        )

        view = MessageRestoreConfirmView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            return

        progress_msg = await interaction.followup.send(
            embed=discord.Embed(
                title="🔄 Nachrichten-Restore startet...",
                color=discord.Color.orange(),
            ),
            ephemeral=False,
        )

        self._msg_restore_task = asyncio.create_task(
            self._run_message_restore_task(
                guild=interaction.guild,
                db_path=self.db_path,
                progress_msg=progress_msg,
                channel_filter=channel,
                limit_per_channel=int(limit) if limit else None,
                match_by_name=match_by_name,
                snapshot_data=snapshot_data,
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BackupCog(bot))
