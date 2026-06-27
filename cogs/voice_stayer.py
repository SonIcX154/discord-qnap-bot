import os
import asyncio
import discord
from discord.ext import commands


class VoiceStayer(commands.Cog):
    """Cog that keeps the bot permanently connected to a specific voice channel.
    
    This makes the voice channel 'long-running' / always active.
    The bot will automatically rejoin if disconnected.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_channel_id = int(os.getenv("VOICE_CHANNEL_ID", "0"))
        self._voice_task = None
        self._running = True

    async def cog_load(self):
        """Start the background task when the cog is loaded."""
        if self.voice_channel_id == 0:
            print("WARNING: VOICE_CHANNEL_ID is not set in .env - voice stayer disabled.")
            return
        self._voice_task = asyncio.create_task(self._stay_in_voice_channel())
        print(f"VoiceStayer initialized. Target channel ID: {self.voice_channel_id}")

    async def cog_unload(self):
        """Clean up the task when cog is unloaded."""
        self._running = False
        if self._voice_task and not self._voice_task.done():
            self._voice_task.cancel()
            try:
                await self._voice_task
            except asyncio.CancelledError:
                pass

    async def _stay_in_voice_channel(self):
        """Background loop that ensures the bot stays connected to the target VC."""
        await self.bot.wait_until_ready()
        print("Voice stayer task started.")

        while self._running and not self.bot.is_closed():
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
                elif not voice_client.is_connected():
                    needs_reconnect = True
                elif voice_client.channel.id != self.voice_channel_id:
                    # Connected to wrong channel - disconnect and move
                    try:
                        await voice_client.disconnect(force=True)
                    except Exception:
                        pass
                    needs_reconnect = True

                if needs_reconnect:
                    try:
                        if voice_client and voice_client.is_connected():
                            await voice_client.disconnect(force=True)
                        await channel.connect(reconnect=True)
                        print(f"✅ [VoiceStayer] Connected to voice channel: {channel.name} ({channel.id})")
                    except discord.ClientException as e:
                        print(f"[VoiceStayer] ClientException while connecting: {e}")
                    except Exception as e:
                        print(f"[VoiceStayer] Error connecting to voice: {e}")

            except Exception as e:
                print(f"[VoiceStayer] Unexpected error: {e}")

            # Check / heal connection every 10 seconds
            await asyncio.sleep(1)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceStayer(bot))
