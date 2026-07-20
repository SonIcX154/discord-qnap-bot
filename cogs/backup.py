from __future__ import annotations

import os
import json
import time
import asyncio
import aiosqlite
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

BACKUP_DB_PATH = os.getenv("BACKUP_DATA_PATH", "data/backup.db")
ATTACHMENTS_DIR = os.getenv("BACKUP_ATTACHMENTS_PATH", "data/backups/attachments")

# Wie viele Nachrichten pro Request (Discord Max = 100)
BACKFILL_BATCH_SIZE = 100
# Kurze Pause zwischen Batches (Rate-Limit freundlich)
BACKFILL_DELAY = 1.1


class BackupCog(commands.Cog):
    """Server Backup System – kontinuierliches Message-Logging + historischer Backfill."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = BACKUP_DB_PATH
        self._catchup_lock = asyncio.Lock()
        self._backfill_task: Optional[asyncio.Task] = None
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

        # Catch-up starten, sobald der Bot ready ist
        self.bot.loop.create_task(self._wait_and_catchup())

    async def cog_unload(self) -> None:
        if self._backfill_task and not self._backfill_task.done():
            self._backfill_task.cancel()

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

    # ==================== MESSAGE LOGGING ====================

    async def _store_message(self, message: discord.Message, *, is_edit: bool = False) -> None:
        """Speichert oder aktualisiert eine Nachricht."""
        if not message.guild:
            return

        # Eigenen Bot nicht speichern
        if self.bot.user and message.author.id == self.bot.user.id:
            return

        embeds_json = (
            json.dumps([e.to_dict() for e in message.embeds], ensure_ascii=False)
            if message.embeds else None
        )

        attachments_data = []
        for att in message.attachments:
            attachments_data.append({
                "filename": att.filename,
                "url": att.url,
                "size": att.size,
                "content_type": att.content_type,
                "local_path": None
            })
        attachments_json = json.dumps(attachments_data, ensure_ascii=False) if attachments_data else None

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

            # Progress aktualisieren (last + oldest)
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

    # ==================== CATCH-UP NACH DOWNTIME ====================

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
        """Holt alle Nachrichten seit der letzten gespeicherten Message (nach vorne)."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT last_message_id FROM channel_progress WHERE channel_id = ?",
                (channel.id,)
            ) as cursor:
                row = await cursor.fetchone()
                last_id = row[0] if row else None

        if last_id is None:
            # Noch nichts da → nur die neuesten 50 als Startpunkt nehmen
            history = channel.history(limit=50, oldest_first=True)
        else:
            history = channel.history(
                after=discord.Object(id=last_id),
                oldest_first=True,
                limit=None
            )

        count = 0
        async for message in history:
            await self._store_message(message)
            count += 1
            if count % 100 == 0:
                await asyncio.sleep(0.5)

        if count:
            print(f"[Backup] #{channel.name}: {count} Nachrichten nachgeholt")

    # ==================== HISTORISCHER BACKFILL ====================

    async def _backfill_channel(self, channel: discord.TextChannel) -> int:
        """
        Holt die komplette History eines Channels von neu nach alt.
        Gibt die Anzahl neu gespeicherter Nachrichten zurück.
        Unterstützt Resume über oldest_message_id.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT oldest_message_id, fully_backfilled FROM channel_progress WHERE channel_id = ?",
                (channel.id,)
            ) as cursor:
                row = await cursor.fetchone()

        if row and row[1] == 1:
            return 0  # schon fertig

        oldest_id: Optional[int] = row[0] if row else None
        total_saved = 0

        while True:
            kwargs = {
                "limit": BACKFILL_BATCH_SIZE,
                "oldest_first": False,  # neueste zuerst
            }
            if oldest_id is not None:
                kwargs["before"] = discord.Object(id=oldest_id)

            batch = [msg async for msg in channel.history(**kwargs)]

            if not batch:
                # Keine älteren Nachrichten mehr → fertig
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

            # batch kommt newest → oldest
            for message in batch:
                await self._store_message(message)
                total_saved += 1

            # neues oldest = die älteste Message in diesem Batch
            oldest_id = batch[-1].id

            # Progress in DB aktualisieren
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

            # Rate-Limit freundlich
            await asyncio.sleep(BACKFILL_DELAY)

        return total_saved

    async def _run_backfill(self, guild: discord.Guild, progress_msg: discord.WebhookMessage | discord.Message) -> None:
        """Hintergrund-Task: geht alle Text-Channels des Servers durch."""
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

                # Progress-Embed aktualisieren
                embed = discord.Embed(
                    title="📦 Historischer Backfill läuft...",
                    color=discord.Color.orange()
                )
                embed.add_field(name="Aktueller Channel", value=f"#{channel.name}", inline=False)
                embed.add_field(name="Fortschritt", value=f"{i-1} / {len(channels)} Channels", inline=True)
                embed.add_field(name="Nachrichten diese Runde", value=f"{self._backfill_status['messages_this_run']:,}", inline=True)
                embed.set_footer(text="Du kannst den Bot weiter benutzen – der Backfill läuft im Hintergrund")

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

            # Fertig
            embed = discord.Embed(
                title="✅ Historischer Backfill abgeschlossen",
                color=discord.Color.green()
            )
            embed.add_field(name="Channels", value=f"{len(channels)}", inline=True)
            embed.add_field(name="Neue Nachrichten", value=f"{self._backfill_status['messages_this_run']:,}", inline=True)
            embed.set_footer(text="Alle Channels sind jetzt als fully_backfilled markiert")
            try:
                await progress_msg.edit(embed=embed)
            except Exception:
                pass

        finally:
            self._backfill_status["running"] = False
            self._backfill_status["current_channel"] = None
            self._backfill_task = None

    # ==================== COMMANDS ====================

    @app_commands.command(name="backup-status", description="Zeigt den aktuellen Backup-Status an")
    @app_commands.default_permissions(administrator=True)
    async def backup_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM messages WHERE is_deleted = 0") as cur:
                total_msgs = (await cur.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM channel_progress") as cur:
                tracked_channels = (await cur.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM channel_progress WHERE fully_backfilled = 1"
            ) as cur:
                fully_done = (await cur.fetchone())[0]

        embed = discord.Embed(title="📦 Backup Status", color=discord.Color.blurple())
        embed.add_field(name="Gespeicherte Nachrichten", value=f"**{total_msgs:,}**", inline=True)
        embed.add_field(name="Überwachte Channels", value=f"**{tracked_channels}**", inline=True)
        embed.add_field(name="Vollständig backfilled", value=f"**{fully_done}**", inline=True)

        if self._backfill_status["running"]:
            embed.add_field(
                name="🔄 Backfill läuft",
                value=(
                    f"Channel: **#{self._backfill_status['current_channel']}**\n"
                    f"Fortschritt: {self._backfill_status['channels_done']} / {self._backfill_status['channels_total']}\n"
                    f"Nachrichten diese Runde: {self._backfill_status['messages_this_run']:,}"
                ),
                inline=False
            )
            embed.color = discord.Color.orange()
        else:
            embed.set_footer(text="Kontinuierliches Logging ist aktiv")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="backup-backfill", description="Startet den historischen Backfill aller Channels")
    @app_commands.default_permissions(administrator=True)
    async def backup_backfill(self, interaction: discord.Interaction) -> None:
        if self._backfill_task and not self._backfill_task.done():
            await interaction.response.send_message(
                "⚠️ Ein Backfill läuft bereits. Schau mit `/backup-status` nach dem Fortschritt.",
                ephemeral=True
            )
            return

        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        embed = discord.Embed(
            title="📦 Historischer Backfill wird gestartet...",
            description="Der Bot holt jetzt die gesamte Nachrichten-History aller Text-Channels.\nDas kann je nach Server-Größe eine Weile dauern.",
            color=discord.Color.orange()
        )
        progress_msg = await interaction.followup.send(embed=embed)

        self._backfill_task = asyncio.create_task(
            self._run_backfill(interaction.guild, progress_msg)
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BackupCog(bot))
