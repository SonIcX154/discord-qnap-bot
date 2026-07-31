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
        description="Overview: latency, cogs, voice, backup, economy, Twitch mirror",
    )
    @app_commands.default_permissions(administrator=True)
    async def status_overview(self, interaction: discord.Interaction) -> None:
        # Method must NOT be named bot_* / cog_* (discord.py restriction)
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

        voice_cog = self.bot.get_cog("VoiceStayer")
        voice_line = "not loaded"
        if voice_cog is not None:
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
            voice_line = (
                f"{'🟢 on' if enabled else '⚪ off'} · "
                f"{'in **#' + ch_name + '**' if connected else 'not connected'}\n"
                f"Target: `{vc_id}`"
            )
        embed.add_field(name="Voice stayer", value=voice_line, inline=True)

        loaded = sorted(self.bot.cogs.keys())
        embed.add_field(
            name="Loaded cogs",
            value=("`" + "`, `".join(loaded) + "`" if loaded else "none"),
            inline=False,
        )

        backup_txt = "DB missing"
        if os.path.isfile(BACKUP_DB_PATH):
            try:
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
                size = _file_size_mb(BACKUP_DB_PATH)
                size_s = f" · {size:.1f} MB" if size is not None else ""
                backup_txt = (
                    f"Active msgs: **{active:,}**\n"
                    f"Soft-deleted: **{deleted:,}**\n"
                    f"Snapshots: **{snaps}**{size_s}"
                )
            except Exception as e:
                backup_txt = f"error: `{e}`"
        embed.add_field(name="Backup", value=backup_txt, inline=True)

        eco_txt = "DB missing"
        if os.path.isfile(ECONOMY_DB_PATH):
            try:
                async with aiosqlite.connect(ECONOMY_DB_PATH) as db:
                    async with db.execute("SELECT COUNT(*) FROM users") as cur:
                        n = int((await cur.fetchone())[0])
                eco_txt = f"Users: **{n:,}**"
                size = _file_size_mb(ECONOMY_DB_PATH)
                if size is not None:
                    eco_txt += f" · {size:.1f} MB"
            except Exception as e:
                eco_txt = f"error: `{e}`"
        embed.add_field(name="Economy", value=eco_txt, inline=True)

        twitch_cog = self.bot.get_cog("TwitchMirrorCog")
        twitch_txt = "not loaded"
        if twitch_cog is not None:
            client = getattr(twitch_cog, "_twitch", None)
            connected = bool(client and getattr(client, "connected", False))
            inbound = len(getattr(client, "_msg_map", {}) or {}) if client else 0
            outbound = len(getattr(client, "_discord_to_twitch", {}) or {}) if client else 0
            db_part = ""
            try:
                store = getattr(twitch_cog, "_store", None)
                if store is not None:
                    counts = await store.count()
                    db_part = (
                        f"\nDB: total **{counts.get('total', 0)}** "
                        f"(in {counts.get('inbound', 0)} / out {counts.get('outbound', 0)})"
                    )
            except Exception:
                if os.path.isfile(TWITCH_MAP_DB_PATH):
                    size = _file_size_mb(TWITCH_MAP_DB_PATH)
                    if size is not None:
                        db_part = f"\nMap DB: {size:.1f} MB"
            twitch_txt = (
                f"{'🟢 connected' if connected else '🔴 down'}\n"
                f"Memory: in **{inbound}** · out **{outbound}**"
                f"{db_part}"
            )
        embed.add_field(name="Twitch mirror", value=twitch_txt, inline=True)

        container_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        embed.set_footer(text=f"Container local time: {container_now}")

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatusCog(bot))
