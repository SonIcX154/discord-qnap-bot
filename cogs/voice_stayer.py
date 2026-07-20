import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional


# ====================== ADMIN CONFIG ======================
# Hardcode YOUR Discord user ID here
ADMIN_USER_ID = 406523291382186004   # <-- REPLACE THIS WITH YOUR REAL DISCORD USER ID


def is_admin_or_manage_guild(interaction: discord.Interaction) -> bool:
    """Allows the hardcoded admin OR anyone with Manage Guild permission."""
    if interaction.user.id == ADMIN_USER_ID:
        return True
    if interaction.guild and isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild:
        return True
    return False
# ==========================================================


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
        self.enabled = False   # Can be toggled with /voice-stayer

    async def cog_load(self) -> None:
        """Start the background task when the cog is loaded."""
        if self.voice_channel_id == 0:
            print("WARNING: VOICE_CHANNEL_ID is not set in .env - voice stayer disabled.")
            return
        self._voice_task = asyncio.create_task(self._stay_in_voice_channel())
        print(f"VoiceStayer initialized. Target channel ID: {self.voice_channel_id}")

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
        description="Toggle the automatic voice stayer on or off (Admin only)"
    )
    async def toggle_voice_stayer(self, interaction: discord.Interaction) -> None:
        self.enabled = not self.enabled

        if self.enabled:
            await interaction.response.send_message(
                "✅ Voice Stayer is now **enabled**. The bot will stay connected to the voice channel.",
                ephemeral=True
            )
        else:
            # Disconnect if currently connected
            voice_client = discord.utils.get(
                self.bot.voice_clients, guild=interaction.guild
            )
            if voice_client and voice_client.is_connected():  # type: ignore[attr-defined]
                try:
                    await voice_client.disconnect(force=True)
                    print(f"[VoiceStayer] Disconnected from voice channel (stayer disabled by {interaction.user})")
                except Exception:
                    pass

            await interaction.response.send_message(
                "❌ Voice Stayer is now **disabled**. The bot will no longer force itself into the voice channel.",
                ephemeral=True
            )

    @toggle_voice_stayer.error
    async def toggle_voice_stayer_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[misc]
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ You need **Manage Guild** permission or be the bot admin to use this command.",
                ephemeral=True
            )
        else:
            raise error

    async def _stay_in_voice_channel(self) -> None:
        """Background loop that ensures the bot stays connected to the target VC."""
        await self.bot.wait_until_ready()
        print("Voice stayer task started.")

        while self._running and not self.bot.is_closed():
            if not self.enabled:
                await asyncio.sleep(5)
                continue

            try:
                channel = self.bot.get_channel(self.voice_channel_id)

                if not channel or not isinstance(channel, discord.VoiceChannel):
                    print(f"[VoiceStayer] Channel {self.voice_channel_id} not found or not a VoiceChannel.")
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
                        print(f"✅ [VoiceStayer] Connected to voice channel: {channel.name} ({channel.id})")
                    except discord.ClientException as e:
                        print(f"[VoiceStayer] ClientException while connecting: {e}")
                    except Exception as e:
                        print(f"[VoiceStayer] Error connecting to voice: {e}")

            except Exception as e:
                print(f"[VoiceStayer] Unexpected error: {e}")

            # Check / heal connection every ~1 second when enabled
            await asyncio.sleep(1)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceStayer(bot))
