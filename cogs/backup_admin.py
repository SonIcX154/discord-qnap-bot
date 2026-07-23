from __future__ import annotations

import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

try:
    from utils.backup_ops import (
        add_excluded_channel,
        remove_excluded_channel,
        list_excluded_channels,
        download_missing_attachments,
        ensure_extra_tables,
    )
except ImportError:
    from ..utils.backup_ops import (
        add_excluded_channel,
        remove_excluded_channel,
        list_excluded_channels,
        download_missing_attachments,
        ensure_extra_tables,
    )

BACKUP_DB_PATH = os.getenv("BACKUP_DATA_PATH", "data/backup.db")
ATTACHMENTS_DIR = os.getenv("BACKUP_ATTACHMENTS_PATH", "data/backups/attachments")


class BackupAdminCog(commands.Cog):
    """Admin-Hilfen für Backup: Exclude-Liste + Missing-Attachments."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = BACKUP_DB_PATH
        self._dl_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        await ensure_extra_tables(self.db_path)

    @app_commands.command(
        name="backup-exclude",
        description="Channel vom Message-Logging/Backfill ausschließen",
    )
    @app_commands.describe(channel="Channel der nicht mehr gesichert werden soll", reason="Optionaler Grund")
    @app_commands.default_permissions(administrator=True)
    async def backup_exclude(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        reason: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        await add_excluded_channel(self.db_path, channel.id, interaction.guild.id, reason)
        await interaction.response.send_message(
            f"✅ **#{channel.name}** wird ab jetzt nicht mehr geloggt/backfilled.",
            ephemeral=True,
        )

    @app_commands.command(
        name="backup-unexclude",
        description="Channel wieder in Backup aufnehmen",
    )
    @app_commands.describe(channel="Channel der wieder gesichert werden soll")
    @app_commands.default_permissions(administrator=True)
    async def backup_unexclude(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        ok = await remove_excluded_channel(self.db_path, channel.id)
        if ok:
            await interaction.response.send_message(
                f"✅ **#{channel.name}** ist wieder im Backup.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ **#{channel.name}** war nicht excluded.",
                ephemeral=True,
            )

    @app_commands.command(
        name="backup-excludes",
        description="Listet ausgeschlossene Channels",
    )
    @app_commands.default_permissions(administrator=True)
    async def backup_excludes(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        rows = await list_excluded_channels(self.db_path, interaction.guild.id)
        if not rows:
            await interaction.response.send_message("Keine excluded Channels.", ephemeral=True)
            return
        lines = []
        for cid, reason in rows:
            ch = interaction.guild.get_channel(cid)
            name = f"#{ch.name}" if ch else f"`{cid}`"
            extra = f" – {reason}" if reason else ""
            lines.append(f"• {name}{extra}")
        embed = discord.Embed(
            title="🚫 Excluded Channels",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="backup-download-missing",
        description="Lädt fehlende Attachments über gespeicherte CDN-URLs nach",
    )
    @app_commands.describe(limit="Max. Nachrichten die geprüft werden (Default 500)")
    @app_commands.default_permissions(administrator=True)
    async def backup_download_missing(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 5000] = 500,
    ) -> None:
        if self._dl_task and not self._dl_task.done():
            await interaction.response.send_message("⚠️ Download läuft bereits.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        progress = await interaction.followup.send(
            embed=discord.Embed(
                title="⬇️ Missing Attachments…",
                description="Starte Download…",
                color=discord.Color.orange(),
            )
        )

        async def run() -> None:
            try:
                async def on_progress(processed: int, downloaded: int, failed: int) -> None:
                    try:
                        await progress.edit(
                            embed=discord.Embed(
                                title="⬇️ Missing Attachments…",
                                description=(
                                    f"Geprüft: **{processed}** Nachrichten\n"
                                    f"Neu geladen: **{downloaded}**\n"
                                    f"Fehler: **{failed}**"
                                ),
                                color=discord.Color.orange(),
                            )
                        )
                    except Exception:
                        pass

                stats = await download_missing_attachments(
                    self.db_path,
                    ATTACHMENTS_DIR,
                    guild_id=interaction.guild_id,
                    limit=int(limit),
                    on_progress=on_progress,
                )
                embed = discord.Embed(
                    title="✅ Missing Attachments fertig",
                    color=discord.Color.green(),
                )
                embed.add_field(name="Geprüft", value=str(stats["checked"]), inline=True)
                embed.add_field(name="Neu geladen", value=str(stats["downloaded"]), inline=True)
                embed.add_field(name="Bereits OK", value=str(stats["already_ok"]), inline=True)
                embed.add_field(name="Fehler", value=str(stats["failed"]), inline=True)
                embed.add_field(name="DB-Updates", value=str(stats["updated_rows"]), inline=True)
                embed.set_footer(text="CDN-URLs können ablaufen – dann bleibt failed")
                await progress.edit(embed=embed)
            except Exception as e:
                await progress.edit(
                    embed=discord.Embed(
                        title="❌ Download fehlgeschlagen",
                        description=f"`{e}`",
                        color=discord.Color.red(),
                    )
                )
            finally:
                self._dl_task = None

        self._dl_task = asyncio.create_task(run())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BackupAdminCog(bot))
