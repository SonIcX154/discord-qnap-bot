from __future__ import annotations

import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Any

try:
    from twitchio.ext import commands as twitch_commands
except ImportError:
    twitch_commands = None  # type: ignore

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
TWITCH_TOKEN = os.getenv("TWITCH_TOKEN", "").strip()
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL", "").strip().lstrip("#").lower()
TWITCH_DISCORD_CHANNEL_ID = os.getenv("TWITCH_DISCORD_CHANNEL_ID", "").strip()
TWITCH_NICK = os.getenv("TWITCH_NICK", "").strip()
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()

# Map Twitch name → Discord user ping (your main account)
# DISCORD_OWNER_ID = your Discord snowflake
# TWITCH_OWNER_NAMES = comma-separated Twitch logins/display names to match
#   (defaults to TWITCH_CHANNEL if only DISCORD_OWNER_ID is set)
# Matches both "@Name" and bare "Name" (word-boundary, case-insensitive)
DISCORD_OWNER_ID = os.getenv("DISCORD_OWNER_ID", "").strip()
TWITCH_OWNER_NAMES = os.getenv("TWITCH_OWNER_NAMES", "").strip()

# Soft rate limit so a busy Twitch chat does not hammer Discord
SEND_DELAY = float(os.getenv("TWITCH_MIRROR_DELAY", "0.35"))

WEBHOOK_NAME = "Twitch Mirror"


def _configured() -> bool:
    return bool(TWITCH_TOKEN and TWITCH_CHANNEL and TWITCH_DISCORD_CHANNEL_ID)


def _normalize_token(token: str) -> str:
    t = token.strip()
    if t.lower().startswith("oauth:"):
        return t
    return f"oauth:{t}"


def _bearer_token(token: str) -> str:
    t = token.strip()
    if t.lower().startswith("oauth:"):
        return t[6:]
    return t


def _safe_webhook_username(name: str) -> str:
    base = (name or "Twitch User").strip() or "Twitch User"
    if base.lower() == "clyde":
        base = "Clyde_"
    return base[:80]


def _build_owner_ping_map() -> dict[str, int]:
    """
    Lowercase Twitch name → Discord user id.

    Env:
      DISCORD_OWNER_ID=123456789012345678
      TWITCH_OWNER_NAMES=Ich_Klau_Gratis_Brot   # optional, default TWITCH_CHANNEL
    """
    mapping: dict[str, int] = {}
    if not DISCORD_OWNER_ID:
        return mapping
    try:
        owner_id = int(DISCORD_OWNER_ID)
    except ValueError:
        print(f"[TwitchMirror] Invalid DISCORD_OWNER_ID: {DISCORD_OWNER_ID!r}")
        return mapping

    raw = TWITCH_OWNER_NAMES or TWITCH_CHANNEL
    for part in raw.split(","):
        name = part.strip().lstrip("@").lower()
        if name:
            mapping[name] = owner_id
    return mapping


OWNER_PING_MAP = _build_owner_ping_map()

# Case-insensitive matcher: optional @, then owner name, word boundary
# e.g. "@Ich_Klau_Gratis_Brot" and "Ich_Klau_Gratis_Brot" both match
_OWNER_MENTION_RE: Optional[re.Pattern[str]] = None
if OWNER_PING_MAP:
    names = sorted(OWNER_PING_MAP.keys(), key=len, reverse=True)
    alternation = "|".join(re.escape(n) for n in names)
    # (?<!\w) avoids matching inside a longer token; @? allows with/without @
    _OWNER_MENTION_RE = re.compile(rf"(?i)(?<!\w)@?({alternation})(?!\w)")


def _apply_owner_pings(content: str) -> tuple[str, list[int]]:
    """
    Replace @Name or bare Name with <@discord_id> for configured owner names.
    Returns (new_content, list of discord user ids that were mentioned).
    """
    if not _OWNER_MENTION_RE or not OWNER_PING_MAP:
        return content, []

    mentioned: list[int] = []

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        uid = OWNER_PING_MAP.get(key)
        if uid is None:
            return match.group(0)
        if uid not in mentioned:
            mentioned.append(uid)
        return f"<@{uid}>"

    return _OWNER_MENTION_RE.sub(repl, content), mentioned


def _allowed_mentions_for(user_ids: list[int]) -> discord.AllowedMentions:
    if not user_ids:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=uid) for uid in user_ids],
    )


class TwitchMirrorBot(twitch_commands.Bot if twitch_commands else object):  # type: ignore[misc]
    """Twitch chat client → Discord webhook (username + avatar per chatter)."""

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
        self._queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self.connected = False
        self._avatar_cache: dict[str, Optional[str]] = {}
        self._avatar_lock = asyncio.Lock()

    async def event_ready(self) -> None:
        self.connected = True
        nick = getattr(self, "nick", None) or TWITCH_NICK or "?"
        print(f"[TwitchMirror] Connected as {nick} → #{TWITCH_CHANNEL}")
        if OWNER_PING_MAP:
            print(
                f"[TwitchMirror] Owner ping map (@ or bare): "
                + ", ".join(f"{k}→<@{v}>" for k, v in OWNER_PING_MAP.items())
            )
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._discord_worker())

    async def event_message(self, message) -> None:  # type: ignore[no-untyped-def]
        if getattr(message, "echo", False):
            return

        author = message.author
        login = (getattr(author, "name", None) or "unknown").lower()
        display = getattr(author, "display_name", None) or getattr(author, "name", None) or "unknown"
        content = (message.content or "").strip()
        if not content:
            return

        await self._queue.put((login, display, content[:2000]))

    async def _fetch_avatar(self, login: str) -> Optional[str]:
        login = login.lower()
        if login in self._avatar_cache:
            return self._avatar_cache[login]

        if not TWITCH_CLIENT_ID or aiohttp is None:
            self._avatar_cache[login] = None
            return None

        async with self._avatar_lock:
            if login in self._avatar_cache:
                return self._avatar_cache[login]

            url = f"https://api.twitch.tv/helix/users?login={login}"
            headers = {
                "Authorization": f"Bearer {_bearer_token(TWITCH_TOKEN)}",
                "Client-Id": TWITCH_CLIENT_ID,
            }
            try:
                timeout = aiohttp.ClientTimeout(total=8)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            print(f"[TwitchMirror] Helix users {login}: HTTP {resp.status}")
                            self._avatar_cache[login] = None
                            return None
                        data: dict[str, Any] = await resp.json()
                users = data.get("data") or []
                if not users:
                    self._avatar_cache[login] = None
                    return None
                avatar = users[0].get("profile_image_url") or None
                self._avatar_cache[login] = avatar
                return avatar
            except Exception as e:
                print(f"[TwitchMirror] Avatar fetch failed for {login}: {e}")
                self._avatar_cache[login] = None
                return None

    async def _get_or_create_webhook(
        self, channel: discord.TextChannel
    ) -> Optional[discord.Webhook]:
        me = channel.guild.me if channel.guild else None
        if me and not channel.permissions_for(me).manage_webhooks:
            print("[TwitchMirror] Missing Manage Webhooks permission")
            return None

        try:
            hooks = await channel.webhooks()
            for h in hooks:
                if h.name == WEBHOOK_NAME and h.token:
                    print(f"[TwitchMirror] Reusing webhook id={h.id}")
                    return h

            hook = await channel.create_webhook(
                name=WEBHOOK_NAME,
                reason="Twitch chat mirror",
            )
            print(f"[TwitchMirror] Created webhook id={hook.id}")
            return hook
        except Exception as e:
            print(f"[TwitchMirror] Webhook setup failed: {e}")
            return None

    async def _discord_worker(self) -> None:
        await self.discord_bot.wait_until_ready()
        channel = self.discord_bot.get_channel(self.discord_channel_id)
        if channel is None:
            try:
                channel = await self.discord_bot.fetch_channel(self.discord_channel_id)
            except Exception as e:
                print(f"[TwitchMirror] Cannot resolve Discord channel {self.discord_channel_id}: {e}")
                return

        if not isinstance(channel, discord.TextChannel):
            print(f"[TwitchMirror] Channel {self.discord_channel_id} is not a text channel")
            return

        webhook = await self._get_or_create_webhook(channel)
        if webhook is None:
            print("[TwitchMirror] Falling back to bot messages (no webhook)")

        print(
            f"[TwitchMirror] Mirroring #{TWITCH_CHANNEL} → #{channel.name} "
            f"(webhook={'yes' if webhook else 'no'})"
        )

        while True:
            try:
                login, display, content = await self._queue.get()
                username = _safe_webhook_username(display)
                avatar_url = await self._fetch_avatar(login)

                content, ping_ids = _apply_owner_pings(content)
                mentions = _allowed_mentions_for(ping_ids)

                if webhook is not None:
                    try:
                        await webhook.send(
                            content=content,
                            username=username,
                            avatar_url=avatar_url or discord.utils.MISSING,
                            wait=False,
                            allowed_mentions=mentions,
                        )
                    except discord.NotFound:
                        print("[TwitchMirror] Webhook missing – recreating")
                        webhook = await self._get_or_create_webhook(channel)
                        if webhook is not None:
                            await webhook.send(
                                content=content,
                                username=username,
                                avatar_url=avatar_url or discord.utils.MISSING,
                                wait=False,
                                allowed_mentions=mentions,
                            )
                    except discord.HTTPException as e:
                        print(f"[TwitchMirror] Webhook send failed: {e}")
                        try:
                            await channel.send(
                                f"**{username}**: {content}",
                                allowed_mentions=mentions,
                            )
                        except Exception as e2:
                            print(f"[TwitchMirror] Fallback send failed: {e2}")
                else:
                    await channel.send(
                        f"**{username}**: {content}",
                        allowed_mentions=mentions,
                    )

                await asyncio.sleep(SEND_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TwitchMirror] Worker error: {e}")
                await asyncio.sleep(1.0)


class TwitchMirrorCog(commands.Cog):
    """Mirrors Twitch chat into Discord via webhook (name + avatar)."""

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

        if not TWITCH_CLIENT_ID:
            print(
                "[TwitchMirror] TWITCH_CLIENT_ID not set – messages will use "
                "Twitch display names but default avatars until you add it"
            )

        self._twitch = TwitchMirrorBot(self.bot, channel_id)
        self._task = asyncio.create_task(self._run_twitch())
        print(f"[TwitchMirror] Starting client for #{TWITCH_CHANNEL}…")

    async def _run_twitch(self) -> None:
        assert self._twitch is not None
        backoff = 5.0
        while True:
            try:
                await self._twitch.start()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TwitchMirror] Connection error: {e} – retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 120.0)
            else:
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
        cached = len(self._twitch._avatar_cache) if self._twitch else 0
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
        embed.add_field(name="Mode", value="Webhook (name + avatar)", inline=True)
        embed.add_field(
            name="Avatar API",
            value="Helix OK" if TWITCH_CLIENT_ID else "no CLIENT_ID (default avatars)",
            inline=True,
        )
        embed.add_field(name="Avatar cache", value=str(cached), inline=True)
        if OWNER_PING_MAP:
            ping_desc = "\n".join(
                f"`@{k}` / `{k}` → <@{v}>" for k, v in OWNER_PING_MAP.items()
            )
            embed.add_field(name="Owner pings", value=ping_desc, inline=False)
        else:
            embed.add_field(
                name="Owner pings",
                value="Off – set `DISCORD_OWNER_ID` to enable",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TwitchMirrorCog(bot))
