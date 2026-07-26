from __future__ import annotations

import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Any
from collections import OrderedDict, defaultdict
from urllib.parse import unquote

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

DISCORD_OWNER_ID = os.getenv("DISCORD_OWNER_ID", "").strip()
TWITCH_OWNER_NAMES = os.getenv("TWITCH_OWNER_NAMES", "").strip()

SEND_DELAY = float(os.getenv("TWITCH_MIRROR_DELAY", "0.35"))
MSG_CACHE_MAX = int(os.getenv("TWITCH_MIRROR_MSG_CACHE", "3000"))

WEBHOOK_NAME = "Twitch Mirror"

_CLEARMSG_RE = re.compile(
    r"target-msg-id=([^;\s]+).*\sCLEARMSG\s",
    re.IGNORECASE,
)
_CLEARCHAT_USER_RE = re.compile(
    r"CLEARCHAT\s+#\S+\s+:(\S+)",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(r"^\x01ACTION[ ]?(.*)\x01$", re.DOTALL | re.IGNORECASE)
_ACTION_FALLBACK_RE = re.compile(
    r"^(?:\x01|\u0001)?ACTION[ ]?(.*?)(?:\x01|\u0001)?$",
    re.DOTALL | re.IGNORECASE,
)


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

_OWNER_MENTION_RE: Optional[re.Pattern[str]] = None
if OWNER_PING_MAP:
    names = sorted(OWNER_PING_MAP.keys(), key=len, reverse=True)
    alternation = "|".join(re.escape(n) for n in names)
    _OWNER_MENTION_RE = re.compile(rf"(?i)(?<!\w)@?({alternation})(?!\w)")


def _apply_owner_pings(content: str) -> tuple[str, list[int]]:
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


def _twitch_msg_id(message: Any) -> Optional[str]:
    mid = getattr(message, "id", None)
    if mid:
        return str(mid)
    tags = getattr(message, "tags", None) or {}
    if isinstance(tags, dict):
        for key in ("id", "msg-id"):
            if tags.get(key):
                return str(tags[key])
    return None


def _message_tags(message: Any) -> dict[str, Any]:
    tags = getattr(message, "tags", None)
    if isinstance(tags, dict):
        return tags
    return {}


def _unescape_twitch_tag(value: str) -> str:
    if not value:
        return ""
    text = unquote(value)
    return (
        text.replace(r"\s", " ")
        .replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\:", ";")
        .replace(r"\\", "\\")
    )


def _normalize_content(raw: str) -> tuple[str, bool]:
    text = (raw or "").strip()
    if not text:
        return "", False

    m = _ACTION_RE.match(text) or _ACTION_FALLBACK_RE.match(text)
    if m:
        body = (m.group(1) or "").strip()
        body = body.replace("\x01", "").replace("\u0001", "").strip()
        return body, True

    if "\x01" in text or "\u0001" in text:
        cleaned = text.replace("\x01", "").replace("\u0001", "")
        if cleaned.upper().startswith("ACTION "):
            return cleaned[7:].strip(), True
        return cleaned.strip(), False

    return text, False


def _parent_names_from_tags(tags: dict[str, Any]) -> list[str]:
    """Login + display name of the message being replied to."""
    names: list[str] = []
    for key in ("reply-parent-user-login", "reply-parent-display-name"):
        raw = tags.get(key)
        if not raw:
            continue
        name = _unescape_twitch_tag(str(raw)).lstrip("@").strip()
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return names


def _strip_leading_reply_mention(content: str, tags: dict[str, Any]) -> str:
    """
    Twitch bots often prefix replies with @ParentName.
    Remove that leading mention when we already show a reply header.
    """
    names = _parent_names_from_tags(tags)
    if not names or not content:
        return content

    # Longest first so display names win over shorter logins when both match
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    # @Name / Name at start, optional : or , then whitespace
    cleaned = re.sub(
        rf"^(?i)@?(?:{alt})\b[:,]?\s+",
        "",
        content,
        count=1,
    ).strip()
    return cleaned or content


def _reply_header_from_tags(tags: dict[str, Any]) -> Optional[str]:
    """Chatterino-style: Replying to Name: parent text"""
    parent_id = tags.get("reply-parent-msg-id")
    if not parent_id:
        return None

    display = (
        tags.get("reply-parent-display-name")
        or tags.get("reply-parent-user-login")
        or "someone"
    )
    display = _unescape_twitch_tag(str(display)).lstrip("@")

    body = tags.get("reply-parent-msg-body") or ""
    body = _unescape_twitch_tag(str(body)).replace("\n", " ").strip()
    body, _ = _normalize_content(body)
    if len(body) > 120:
        body = body[:117] + "…"

    if body:
        return f"-# ↩️ Replying to **{display}**: {body}"
    return f"-# ↩️ Replying to **{display}**"


class TwitchMirrorBot(twitch_commands.Bot if twitch_commands else object):  # type: ignore[misc]
    """Twitch chat → Discord webhook, with moderated-message deletes."""

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
        self._queue: asyncio.Queue[
            tuple[str, str, str, Optional[str], Optional[str], bool]
        ] = asyncio.Queue()
        self._delete_queue: asyncio.Queue[tuple[str, Optional[str]]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self.connected = False
        self._avatar_cache: dict[str, Optional[str]] = {}
        self._avatar_lock = asyncio.Lock()

        self._msg_map: OrderedDict[str, int] = OrderedDict()
        self._login_msgs: dict[str, set[str]] = defaultdict(set)
        self._parent_discord: OrderedDict[str, int] = OrderedDict()
        self._webhook: Optional[discord.Webhook] = None
        self._text_channel: Optional[discord.TextChannel] = None

    def _remember(self, twitch_id: str, discord_id: int, login: str) -> None:
        self._msg_map[twitch_id] = discord_id
        self._msg_map.move_to_end(twitch_id)
        self._login_msgs[login.lower()].add(twitch_id)
        while len(self._msg_map) > MSG_CACHE_MAX:
            old_tid, _ = self._msg_map.popitem(last=False)
            for s in self._login_msgs.values():
                s.discard(old_tid)

    def _forget_twitch_id(self, twitch_id: str) -> Optional[int]:
        discord_id = self._msg_map.pop(twitch_id, None)
        for s in self._login_msgs.values():
            s.discard(twitch_id)
        return discord_id

    async def event_ready(self) -> None:
        self.connected = True
        nick = getattr(self, "nick", None) or TWITCH_NICK or "?"
        print(f"[TwitchMirror] Connected as {nick} → #{TWITCH_CHANNEL}")
        if OWNER_PING_MAP:
            print(
                f"[TwitchMirror] Owner ping map (@ or bare): "
                + ", ".join(f"{k}→<@{v}>" for k, v in OWNER_PING_MAP.items())
            )
        print("[TwitchMirror] Moderation sync: CLEARMSG + CLEARCHAT → Discord deletes")
        print("[TwitchMirror] /me plain · replies: Chatterino-style + strip parent @")
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._discord_worker())

    async def event_message(self, message) -> None:  # type: ignore[no-untyped-def]
        if getattr(message, "echo", False):
            return

        author = message.author
        login = (getattr(author, "name", None) or "unknown").lower()
        display = getattr(author, "display_name", None) or getattr(author, "name", None) or "unknown"
        raw = message.content or ""
        content, is_action = _normalize_content(raw)
        if not content:
            return

        tags = _message_tags(message)
        reply_header = _reply_header_from_tags(tags)
        if reply_header:
            content = _strip_leading_reply_mention(content, tags)
            if not content:
                return

        await self._queue.put(
            (login, display, content[:1900], _twitch_msg_id(message), reply_header, is_action)
        )

    async def event_raw_data(self, data: str) -> None:  # type: ignore[no-untyped-def]
        if not data:
            return

        if "CLEARMSG" in data:
            m = _CLEARMSG_RE.search(data)
            if m:
                tid = m.group(1).strip()
                if tid:
                    await self._delete_queue.put(("id", tid))
            return

        if "CLEARCHAT" in data:
            m = _CLEARCHAT_USER_RE.search(data)
            if m:
                login = m.group(1).strip().lower()
                await self._delete_queue.put(("user", login))
                return
            if re.search(r"CLEARCHAT\s+#\S+\s*$", data.strip(), re.IGNORECASE):
                await self._delete_queue.put(("all", None))

    async def event_message_delete(self, message) -> None:  # type: ignore[no-untyped-def]
        tid = _twitch_msg_id(message)
        if tid:
            await self._delete_queue.put(("id", tid))

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

    async def _delete_discord_message(self, discord_msg_id: int) -> None:
        webhook = self._webhook
        if webhook is not None and webhook.token:
            try:
                await webhook.delete_message(discord_msg_id)
                return
            except discord.NotFound:
                return
            except Exception as e:
                print(f"[TwitchMirror] Webhook delete failed: {e}")

        channel = self._text_channel
        if channel is not None:
            try:
                msg = await channel.fetch_message(discord_msg_id)
                await msg.delete()
            except Exception as e:
                print(f"[TwitchMirror] Channel delete failed: {e}")

    async def _process_delete(self, kind: str, value: Optional[str]) -> None:
        if kind == "id" and value:
            discord_id = self._forget_twitch_id(value)
            if discord_id is not None:
                await self._delete_discord_message(discord_id)
                print(f"[TwitchMirror] Deleted mirrored msg (CLEARMSG {value[:8]}…)")
            return

        if kind == "user" and value:
            login = value.lower()
            tids = list(self._login_msgs.get(login, set()))
            deleted = 0
            for tid in tids:
                discord_id = self._forget_twitch_id(tid)
                if discord_id is not None:
                    await self._delete_discord_message(discord_id)
                    deleted += 1
                    await asyncio.sleep(0.25)
            if deleted:
                print(f"[TwitchMirror] CLEARCHAT @{login}: removed {deleted} Discord msg(s)")
            return

        if kind == "all":
            items = list(self._msg_map.items())
            self._msg_map.clear()
            self._login_msgs.clear()
            deleted = 0
            for _, discord_id in items:
                await self._delete_discord_message(discord_id)
                deleted += 1
                await asyncio.sleep(0.25)
            if deleted:
                print(f"[TwitchMirror] Full CLEARCHAT: removed {deleted} Discord msg(s)")

    def _compose_content(
        self,
        content: str,
        reply_header: Optional[str],
        is_action: bool,
    ) -> str:
        body = content
        if reply_header:
            return f"{reply_header}\n{body}"
        return body

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

        self._text_channel = channel
        webhook = await self._get_or_create_webhook(channel)
        self._webhook = webhook
        if webhook is None:
            print("[TwitchMirror] Falling back to bot messages (no webhook)")

        print(
            f"[TwitchMirror] Mirroring #{TWITCH_CHANNEL} → #{channel.name} "
            f"(webhook={'yes' if webhook else 'no'})"
        )

        while True:
            try:
                try:
                    kind, value = self._delete_queue.get_nowait()
                    await self._process_delete(kind, value)
                    continue
                except asyncio.QueueEmpty:
                    pass

                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    get_msg = asyncio.create_task(self._queue.get())
                    get_del = asyncio.create_task(self._delete_queue.get())
                    done, pending = await asyncio.wait(
                        {get_msg, get_del}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    task = next(iter(done))
                    result = task.result()
                    if task is get_del:
                        kind, value = result
                        await self._process_delete(kind, value)
                        continue
                    item = result

                login, display, content, twitch_id, reply_header, is_action = item
                username = _safe_webhook_username(display)
                avatar_url = await self._fetch_avatar(login)

                content, ping_ids = _apply_owner_pings(content)
                final = self._compose_content(content, reply_header, is_action)
                mentions = _allowed_mentions_for(ping_ids)

                discord_msg_id: Optional[int] = None

                if webhook is not None:
                    try:
                        sent = await webhook.send(
                            content=final,
                            username=username,
                            avatar_url=avatar_url or discord.utils.MISSING,
                            wait=True,
                            allowed_mentions=mentions,
                        )
                        discord_msg_id = sent.id
                    except discord.NotFound:
                        print("[TwitchMirror] Webhook missing – recreating")
                        webhook = await self._get_or_create_webhook(channel)
                        self._webhook = webhook
                        if webhook is not None:
                            sent = await webhook.send(
                                content=final,
                                username=username,
                                avatar_url=avatar_url or discord.utils.MISSING,
                                wait=True,
                                allowed_mentions=mentions,
                            )
                            discord_msg_id = sent.id
                    except discord.HTTPException as e:
                        print(f"[TwitchMirror] Webhook send failed: {e}")
                        try:
                            sent = await channel.send(
                                f"**{username}**: {final}",
                                allowed_mentions=mentions,
                            )
                            discord_msg_id = sent.id
                        except Exception as e2:
                            print(f"[TwitchMirror] Fallback send failed: {e2}")
                else:
                    sent = await channel.send(
                        f"**{username}**: {final}",
                        allowed_mentions=mentions,
                    )
                    discord_msg_id = sent.id

                if twitch_id and discord_msg_id is not None:
                    self._remember(twitch_id, discord_msg_id, login)

                await asyncio.sleep(SEND_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TwitchMirror] Worker error: {e}")
                await asyncio.sleep(1.0)


class TwitchMirrorCog(commands.Cog):
    """Mirrors Twitch chat into Discord via webhook (name + avatar + mod deletes)."""

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
        tracked = len(self._twitch._msg_map) if self._twitch else 0
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
        embed.add_field(name="Mode", value="Webhook + mod deletes + replies", inline=True)
        embed.add_field(
            name="Avatar API",
            value="Helix OK" if TWITCH_CLIENT_ID else "no CLIENT_ID (default avatars)",
            inline=True,
        )
        embed.add_field(name="Tracked msgs", value=str(tracked), inline=True)
        if OWNER_PING_MAP:
            ping_desc = "\n".join(
                f"`@{k}` / `{k}` → <@{v}>" for k, v in OWNER_PING_MAP.items()
            )
            embed.add_field(name="Owner pings", value=ping_desc, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TwitchMirrorCog(bot))
