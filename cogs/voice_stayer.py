from __future__ import annotations

import os
import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

log = logging.getLogger("qnapbot.voice_stayer")


def _parse_bot_dev_ids() -> set[int]:
    """Parse BOT_DEV_ID / BOT_DEV_IDS (comma or semicolon separated)."""
    raw = (os.getenv("BOT_DEV_ID") or os.getenv("BOT_DEV_IDS") or "").strip()
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


BOT_DEV_IDS: set[int] = _parse_bot_dev_ids()


def is_admin_or_manage_guild(interaction: discord.Interaction) -> bool:
    """Allows BOT_DEV_ID(s) OR anyone with Manage Guild permission."""
    if interaction.user.id in BOT_DEV_IDS:
        return True
    if (
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    ):
        return True
    return False


class VoiceStayer(commands.Cog):
    """Cog that keeps the bot permanently connected to a specific voice channel.

    This makes the voice channel 'long-running' / always active.
    The bot will automatically rejoin if disconnected.
    Use /voice-stayer to toggle this behavior on or off.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.voice_channel_id = int(os.getenv("VOICE_CHANNEL_ID", "0"))
        self._voice_task: Optional[asyncio.Task[None]] = None
        self._running = True
        self.enabled = False  # Can be toggled with /voice-stayer

    async def cog_load(self) -> None:
        """Start the background task when the cog is loaded."""
        if self.voice_channel_id == 0:
            log.warning("VOICE_CHANNEL_ID is not set in .env – voice stayer disabled")
            return
        if BOT_DEV_IDS:
            log.info("BOT_DEV_ID(s) loaded: %s", ", ".join(str(i) for i in sorted(BOT_DEV_IDS)))
        else:
            log.info("No BOT_DEV_ID set – only Manage Guild can toggle voice-stayer")
        self._voice_task = asyncio.create_task(self._stay_in_voice_channel())
        log.info("VoiceStayer initialized (target channel ID: %s)", self.voice_channel_id)

    async def cog_unload(self) -> None:
        """Clean up the task when the cog is unloaded."""
        self._running = False
        if self._voice_task and not self._voice_task.done():
            self._voice_task.cancel()
            try:
                await self._voice_task
            except asyncio.CancelledError:
                pass

    @app_commands.check(is_admin_or_manage_guild)
    @app_commands.command(
        name="voice-stayer",
        description="Toggle the automatic voice stayer on or off (Admin / Bot-Dev only)",
    )
    async def toggle_voice_stayer(self, interaction: discord.Interaction) -> None:
        self.enabled = not self.enabled

        if self.enabled:
            await interaction.response.send_message(
                "✅ Voice Stayer is now **enabled**. The bot will stay connected to the voice channel.",
                ephemeral=True,
            )
        else:
            # Disconnect if currently connected
            voice_client = discord.utils.get(
                self.bot.voice_clients, guild=interaction.guild
            )
            if voice_client and voice_client.is_connected():  # type: ignore[attr-defined]
                try:
                    await voice_client.disconnect(force=True)
                    log.info(
                        "Disconnected from voice channel (stayer disabled by %s)",
                        interaction.user,
                    )
                except Exception:
                    pass

            await interaction.response.send_message(
                "❌ Voice Stayer is now **disabled**. The bot will no longer force itself into the voice channel.",
                ephemeral=True,
            )

    @toggle_voice_stayer.error
    async def toggle_voice_stayer_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:  # type: ignore[misc]
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ Du brauchst **Manage Guild** oder musst als **Bot-Dev** hinterlegt sein.",
                ephemeral=True,
            )
        else:
            raise error

    async def _stay_in_voice_channel(self) -> None:
        """Background loop that ensures the bot stays connected to the target VC."""
        await self.bot.wait_until_ready()
        log.info("Voice stayer task started")

        while self._running and not self.bot.is_closed():
            if not self.enabled:
                await asyncio.sleep(5)
                continue

            try:
                channel = self.bot.get_channel(self.voice_channel_id)

                if not channel or not isinstance(channel, discord.VoiceChannel):
                    log.warning(
                        "Channel %s not found or not a VoiceChannel", self.voice_channel_id
                    )
                    await asyncio.sleep(60)
                    continue

                # Find if we're already connected in this guild
                voice_client = discord.utils.get(
                    self.bot.voice_clients, guild=channel.guild
                )

                needs_reconnect = False

                if voice_client is None:
                    needs_reconnect = True
                elif not voice_client.is_connected():  # type: ignore[attr-defined]
                    needs_reconnect = True
                elif voice_client.channel.id != self.voice_channel_id:  # type: ignore[attr-defined]
                    # Connected to wrong channel - disconnect and move
                    try:
                        await voice_client.disconnect(force=True)
                    except Exception:
                        pass
                    needs_reconnect = True

                if needs_reconnect:
                    try:
                        if voice_client and voice_client.is_connected():  # type: ignore[attr-defined]
                            await voice_client.disconnect(force=True)
                        await channel.connect(reconnect=True)
                        log.info(
                            "Connected to voice channel: %s (%s)", channel.name, channel.id
                        )
                    except discord.ClientException as e:
                        log.warning("ClientException while connecting: %s", e)
                    except Exception as e:
                        log.error("Error connecting to voice: %s", e)

            except Exception as e:
                log.exception("Unexpected error in voice stayer: %s", e)

            # Check / heal connection every ~1 second when enabled
            await asyncio.sleep(1)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceStayer(bot))
