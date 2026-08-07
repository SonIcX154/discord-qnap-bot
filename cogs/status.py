from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

BACKUP_DB_PATH = os.getenv("BACKUP_DATA_PATH", "data/backup.db")
ECONOMY_DB_PATH = os.getenv("ECONOMY_DATA_PATH", "data/economy.db")
TWITCH_MAP_DB_PATH = os.getenv("TWITCH_MIRROR_DB_PATH", "data/twitch_mirror.db")
VOICE_CHANNEL_ID = os.getenv("VOICE_CHANNEL_ID", "").strip()
BADGEBASE_API_KEY = os.getenv("BADGEBASE_API_KEY", "").strip()


def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _file_size_mb(path: str) -> Optional[float]:
    try:
        if os.path.isfile(path):
            return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        pass
    return None


class StatusCog(commands.Cog):
    """Unified health / status overview for admins."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="bot-status",
        description="Overview: latency, cogs, voice, backup, economy, Twitch, BadgeBase",
    )
    @app_commands.default_permissions(administrator=True)
    async def status_overview(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        latency_ms = round(self.bot.latency * 1000)
        start_ts = getattr(self.bot, "start_time", None)
        uptime = _fmt_uptime(time.time() - start_ts) if start_ts else "?"

        embed = discord.Embed(
            title="🤖 Bot Status",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Core",
            value=(
                f"Latency: **{latency_ms} ms**\n"
                f"Uptime: **{uptime}**\n"
                f"Guilds: **{len(self.bot.guilds)}**\n"
                f"Cogs: **{len(self.bot.cogs)}**"
            ),
            inline=True,
        )

        embed.add_field(name="Voice stayer", value=self._voice_line(), inline=True)
        embed.add_field(name="Twitch mirror", value=await self._twitch_line(), inline=True)
        embed.add_field(name="Backup", value=await self._backup_line(), inline=True)
        embed.add_field(name="Economy", value=await self._economy_line(), inline=True)
        embed.add_field(name="BadgeBase", value=await self._badgebase_line(interaction), inline=True)

        loaded = sorted(self.bot.cogs.keys())
        embed.add_field(
            name="Loaded cogs",
            value=("`" + "`, `".join(loaded) + "`" if loaded else "none"),
            inline=False,
        )

        container_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        embed.set_footer(text=f"Container local time: {container_now}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    def _voice_line(self) -> str:
        try:
            voice_cog = self.bot.get_cog("VoiceStayer")
            if voice_cog is None:
                return "not loaded"
            enabled = bool(getattr(voice_cog, "enabled", False))
            vc_id = getattr(voice_cog, "voice_channel_id", 0) or VOICE_CHANNEL_ID
            connected = False
            ch_name = "?"
            for vc in self.bot.voice_clients:
                if getattr(vc, "is_connected", lambda: False)():
                    ch = getattr(vc, "channel", None)
                    if ch is not None:
                        connected = True
                        ch_name = getattr(ch, "name", str(ch.id))
                        break
            return (
                f"{'🟢 on' if enabled else '⚪ off'} · "
                f"{'in **#' + ch_name + '**' if connected else 'not connected'}\n"
                f"Target: `{vc_id}`"
            )
        except Exception as e:
            return f"error: `{e}`"

    async def _twitch_line(self) -> str:
        try:
            twitch_cog = self.bot.get_cog("TwitchMirrorCog")
            if twitch_cog is None:
                return "not loaded"

            client = getattr(twitch_cog, "_twitch", None)
            connected = bool(client and getattr(client, "connected", False))
            inbound = 0
            outbound = 0
            idle_s = None
            if client is not None:
                try:
                    inbound = len(getattr(client, "_msg_map", {}) or {})
                    outbound = len(getattr(client, "_discord_to_twitch", {}) or {})
                    last = getattr(client, "_last_irc_activity", None)
                    if last is not None:
                        idle_s = int(max(0, time.time() - float(last)))
                except Exception:
                    pass

            db_part = ""
            try:
                store = getattr(twitch_cog, "_store", None)
                if store is not None and hasattr(store, "count"):
                    counts = await store.count()
                    if isinstance(counts, dict):
                        db_part = (
                            f"\nDB: **{counts.get('total', 0)}** "
                            f"(in {counts.get('inbound', 0)} / out {counts.get('outbound', 0)})"
                        )
            except Exception:
                size = _file_size_mb(TWITCH_MAP_DB_PATH)
                if size is not None:
                    db_part = f"\nMap DB: {size:.1f} MB"

            idle_part = f"\nIRC idle: `{idle_s}s`" if idle_s is not None else ""
            return (
                f"{'🟢 connected' if connected else '🔴 down'}\n"
                f"Memory: in **{inbound}** · out **{outbound}**"
                f"{idle_part}{db_part}"
            )
        except Exception as e:
            return f"unavailable (`{e}`)"

    async def _backup_line(self) -> str:
        try:
            if self.bot.get_cog("BackupCog") is None:
                return "not loaded"

            if not os.path.isfile(BACKUP_DB_PATH):
                return "DB missing"

            async with aiosqlite.connect(BACKUP_DB_PATH) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM messages WHERE is_deleted = 0"
                ) as cur:
                    active = int((await cur.fetchone())[0])
                async with db.execute(
                    "SELECT COUNT(*) FROM messages WHERE is_deleted = 1"
                ) as cur:
                    deleted = int((await cur.fetchone())[0])
                async with db.execute("SELECT COUNT(*) FROM snapshots") as cur:
                    snaps = int((await cur.fetchone())[0])
                fully = 0
                tracked = 0
                try:
                    async with db.execute("SELECT COUNT(*) FROM channel_progress") as cur:
                        tracked = int((await cur.fetchone())[0])
                    async with db.execute(
                        "SELECT COUNT(*) FROM channel_progress WHERE fully_backfilled = 1"
                    ) as cur:
                        fully = int((await cur.fetchone())[0])
                except Exception:
                    pass

            size = _file_size_mb(BACKUP_DB_PATH)
            size_s = f" · {size:.1f} MB" if size is not None else ""

            running = ""
            try:
                backup_cog = self.bot.get_cog("BackupCog")
                if backup_cog is not None:
                    st = getattr(backup_cog, "_backfill_status", None) or {}
                    if st.get("running"):
                        running = (
                            f"\n🔄 Sync: #{st.get('current_channel') or '?'} "
                            f"({st.get('channels_done', 0)}/{st.get('channels_total', 0)})"
                        )
                    elif getattr(backup_cog, "_restore_task", None) and not backup_cog._restore_task.done():
                        running = "\n🔄 Struktur-Restore läuft"
                    elif getattr(backup_cog, "_msg_restore_task", None) and not backup_cog._msg_restore_task.done():
                        running = "\n🔄 Nachrichten-Restore läuft"
            except Exception:
                pass

            return (
                f"Active: **{active:,}** · deleted: **{deleted:,}**\n"
                f"Channels: **{fully}/{tracked}** backfilled · snaps **{snaps}**{size_s}"
                f"{running}"
            )
        except Exception as e:
            return f"error: `{e}`"

    async def _economy_line(self) -> str:
        try:
            if self.bot.get_cog("EconomyCog") is None:
                return "not loaded"

            if not os.path.isfile(ECONOMY_DB_PATH):
                return "DB missing"
            async with aiosqlite.connect(ECONOMY_DB_PATH) as db:
                async with db.execute("SELECT COUNT(*) FROM users") as cur:
                    n = int((await cur.fetchone())[0])
            eco_txt = f"Users: **{n:,}**"
            size = _file_size_mb(ECONOMY_DB_PATH)
            if size is not None:
                eco_txt += f" · {size:.1f} MB"
            return eco_txt
        except Exception as e:
            return f"error: `{e}`"

    async def _badgebase_line(self, interaction: discord.Interaction) -> str:
        try:
            cog = self.bot.get_cog("BadgeBaseCog")
            if cog is None:
                return "not loaded"

            key_ok = bool(BADGEBASE_API_KEY)
            known_n = "?"
            try:
                seen = getattr(cog, "seen", None)
                if seen is not None and hasattr(seen, "known_ids"):
                    known_n = str(len(await seen.known_ids()))
            except Exception:
                pass

            channel_txt = "*not set*"
            try:
                if interaction.guild:
                    settings = getattr(cog, "settings", None)
                    key = "badgebase.notify_channel"
                    if settings is not None and hasattr(settings, "get_channel"):
                        cid = await settings.get_channel(interaction.guild.id, key)
                        if cid:
                            channel_txt = f"<#{cid}>"
            except Exception:
                pass

            poll = "?"
            try:
                import cogs.badgebase as bb

                poll = getattr(bb, "POLL_SECONDS", "?")
            except Exception:
                pass

            return (
                f"API: {'✅' if key_ok else '❌'} · known **{known_n}**\n"
                f"Poll: `{poll}s` · channel: {channel_txt}"
            )
        except Exception as e:
            return f"unavailable (`{e}`)"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatusCog(bot))
