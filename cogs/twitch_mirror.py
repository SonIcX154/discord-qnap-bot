from __future__ import annotations

import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

try:
    from twitchio.ext import commands as twitch_commands
except ImportError:
    twitch_commands = None  # type: ignore


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
TWITCH_TOKEN = os.getenv("TWITCH_TOKEN", "").strip()
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL", "").strip().lstrip("#").lower()
TWITCH_DISCORD_CHANNEL_ID = os.getenv("TWITCH_DISCORD_CHANNEL_ID", "").strip()
TWITCH_NICK = os.getenv("TWITCH_NICK", "").strip()  # optional display/login hint

# Soft rate limit so a busy Twitch chat does not hammer Discord
SEND_DELAY = float(os.getenv("TWITCH_MIRROR_DELAY", "0.35"))


def _configured() -> bool:
    return bool(TWITCH_TOKEN and TWITCH_CHANNEL and TWITCH_DISCORD_CHANNEL_ID)


def _normalize_token(token: str) -> str:
    """twitchio accepts oauth:xxx or bare access token."""
    t = token.strip()
    if t.lower().startswith("oauth:"):
        return t
    return f"oauth:{t}"


class TwitchMirrorBot(twitch_commands.Bot if twitch_commands else object):  # type: ignore[misc]
    """Minimal Twitch chat client that forwards messages to a Discord channel."""

    def __init__(self, discord_bot: commands.Bot, discord_channel_id: int) -> None:
        if twitch_commands is None:
            raise RuntimeError("twitchio is not installed")

        super().__init__(
            token=_normalize_token(TWITCH_TOKEN),
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
        )
        self.discord_bot = discord_bot
        self.discord_channel_id = discord_channel_id
        self._queue: asyncio.Queue[tuple[str, str, bool]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self.connected = False

    async def event_ready(self) -> None:
        self.connected = True
        nick = getattr(self, "nick", None) or TWITCH_NICK or "?"
        print(f"[TwitchMirror] Connected as {nick} → #{TWITCH_CHANNEL}")
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._discord_worker())

    async def event_message(self, message) -> None:  # type: ignore[no-untyped-def]
        # Ignore messages the bot itself echoed / sent
        if getattr(message, "echo", False):
            return
        author = getattr(message.author, "name", None) or "unknown"
        content = (message.content or "").strip()
        if not content:
            return

        # Optional: skip commands-only noise from other bots starting with !
        # (comment out if you want those mirrored too)
        # if content.startswith("!"):
        #     return

        is_mod = bool(getattr(message.author, "is_mod", False))
        is_sub = bool(getattr(message.author, "is_subscriber", False))
        badges = []
        if is_mod:
            badges.append("mod")
        if is_sub:
            badges.append("sub")
        if getattr(message.author, "is_broadcaster", False):
            badges.append("streamer")

        display = author
        if badges:
            display = f"[{'|'.join(badges)}] {author}"

        await self._queue.put((display, content[:1900], is_mod or getattr(message.author, "is_broadcaster", False)))

    async def _discord_worker(self) -> None:
        """Drain the queue and post to Discord with a small delay."""
        await self.discord_bot.wait_until_ready()
        channel = self.discord_bot.get_channel(self.discord_channel_id)
        if channel is None:
            try:
                channel = await self.discord_bot.fetch_channel(self.discord_channel_id)
            except Exception as e:
                print(f"[TwitchMirror] Cannot resolve Discord channel {self.discord_channel_id}: {e}")
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            print(f"[TwitchMirror] Channel {self.discord_channel_id} is not a text channel")
            return

        print(f"[TwitchMirror] Mirroring #{TWITCH_CHANNEL} → #{getattr(channel, 'name', channel.id)}")

        while True:
            try:
                display, content, highlight = await self._queue.get()
                color = discord.Color.purple() if highlight else discord.Color.dark_purple()
                embed = discord.Embed(
                    description=content,
                    color=color,
                )
                embed.set_author(
                    name=f"{display} · Twitch",
                    icon_url="https://static-cdn.jtvnw.net/jtv_user_pictures/panel-Twitch-profile_image-0f5d5b5b5b5b5b5b-profile_image-70x70.png",
                )
                embed.set_footer(text=f"#{TWITCH_CHANNEL}")
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException as e:
                    # Fallback to plain text if embed fails
                    print(f"[TwitchMirror] Discord send failed: {e}")
                    try:
                        await channel.send(f"**{display}**: {content}")
                    except Exception as e2:
                        print(f"[TwitchMirror] Plain send failed: {e2}")
                await asyncio.sleep(SEND_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TwitchMirror] Worker error: {e}")
                await asyncio.sleep(1.0)


class TwitchMirrorCog(commands.Cog):
    """Mirrors Twitch chat messages into a Discord text channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._twitch: Optional[TwitchMirrorBot] = None
        self._task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        if twitch_commands is None:
            print("[TwitchMirror] twitchio not installed – cog idle (pip install twitchio)")
            return
        if not _configured():
            print(
                "[TwitchMirror] Disabled – set TWITCH_TOKEN, TWITCH_CHANNEL, "
                "TWITCH_DISCORD_CHANNEL_ID in .env to enable"
            )
            return

        try:
            channel_id = int(TWITCH_DISCORD_CHANNEL_ID)
        except ValueError:
            print(f"[TwitchMirror] Invalid TWITCH_DISCORD_CHANNEL_ID: {TWITCH_DISCORD_CHANNEL_ID!r}")
            return

        self._twitch = TwitchMirrorBot(self.bot, channel_id)
        self._task = asyncio.create_task(self._run_twitch())
        print(f"[TwitchMirror] Starting client for #{TWITCH_CHANNEL}…")

    async def _run_twitch(self) -> None:
        assert self._twitch is not None
        backoff = 5.0
        while True:
            try:
                # twitchio Bot.start() connects and runs until closed
                await self._twitch.start()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TwitchMirror] Connection error: {e} – retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 120.0)
            else:
                # Clean exit – wait and reconnect
                print("[TwitchMirror] Disconnected – reconnecting in 10s")
                await asyncio.sleep(10.0)
                backoff = 5.0

    async def cog_unload(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._twitch is not None:
            try:
                await self._twitch.close()
            except Exception:
                pass
            if self._twitch._worker_task and not self._twitch._worker_task.done():
                self._twitch._worker_task.cancel()

    @app_commands.command(
        name="twitch-mirror-status",
        description="Shows Twitch chat mirror status",
    )
    @app_commands.default_permissions(administrator=True)
    async def twitch_mirror_status(self, interaction: discord.Interaction) -> None:
        if not _configured():
            await interaction.response.send_message(
                "Twitch mirror is **not configured**.\n"
                "Set `TWITCH_TOKEN`, `TWITCH_CHANNEL`, `TWITCH_DISCORD_CHANNEL_ID` in `.env`.",
                ephemeral=True,
            )
            return

        connected = bool(self._twitch and self._twitch.connected)
        embed = discord.Embed(
            title="Twitch Mirror Status",
            color=discord.Color.green() if connected else discord.Color.orange(),
        )
        embed.add_field(name="Twitch channel", value=f"`#{TWITCH_CHANNEL}`", inline=True)
        embed.add_field(
            name="Discord channel",
            value=f"<#{TWITCH_DISCORD_CHANNEL_ID}>",
            inline=True,
        )
        embed.add_field(
            name="Connection",
            value="🟢 connected" if connected else "🔴 disconnected / starting",
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TwitchMirrorCog(bot))
