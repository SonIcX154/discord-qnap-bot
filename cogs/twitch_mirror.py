from __future__ import annotations

import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Any, Deque
from collections import OrderedDict, defaultdict, deque
from urllib.parse import unquote

try:
    from twitchio.ext import commands as twitch_commands
except ImportError:
    twitch_commands = None  # type: ignore

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore

try:
    from utils.twitch_map_store import TwitchMapStore, DEFAULT_PATH as TWITCH_MAP_DEFAULT_PATH
except ImportError:
    from ..utils.twitch_map_store import TwitchMapStore, DEFAULT_PATH as TWITCH_MAP_DEFAULT_PATH


TWITCH_TOKEN = os.getenv("TWITCH_TOKEN", "").strip()
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL", "").strip().lstrip("#").lower()
TWITCH_DISCORD_CHANNEL_ID = os.getenv("TWITCH_DISCORD_CHANNEL_ID", "").strip()
TWITCH_NICK = os.getenv("TWITCH_NICK", "").strip()
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()

DISCORD_OWNER_ID = os.getenv("DISCORD_OWNER_ID", "").strip()
TWITCH_OWNER_NAMES = os.getenv("TWITCH_OWNER_NAMES", "").strip()

_RAW_D2T = os.getenv("TWITCH_DISCORD_TO_TWITCH", "1").strip().lower()
DISCORD_TO_TWITCH = _RAW_D2T not in ("0", "false", "no", "off")

SEND_DELAY = float(os.getenv("TWITCH_MIRROR_DELAY", "0.35"))
MSG_CACHE_MAX = int(os.getenv("TWITCH_MIRROR_MSG_CACHE", "3000"))
TWITCH_MIRROR_DB_PATH = os.getenv("TWITCH_MIRROR_DB_PATH", TWITCH_MAP_DEFAULT_PATH)

WEBHOOK_NAME = "Twitch Mirror"
REQUIRED_DELETE_SCOPE = "moderator:manage:chat_messages"
REQUIRED_SEND_SCOPE = "user:write:chat"

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
    r"^(?:\x01|\u0001)ACTION[ ]?(.*?)(?:\x01|\u0001)$",
    re.DOTALL | re.IGNORECASE,
)
_LEADING_AT_RE = re.compile(r"^@\S+\s+")
_DISCORD_MENTION_RE = re.compile(r"<@!?(\d+)>")
_DISCORD_ROLE_RE = re.compile(r"<@&(\d+)>")
_DISCORD_CHANNEL_RE = re.compile(r"<#(\d+)>")
_DISCORD_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")


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
    if tags is not None and hasattr(tags, "items"):
        try:
            return dict(tags.items())  # type: ignore[arg-type]
        except Exception:
            pass
    return {}


def _tag_get(tags: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        if key in tags and tags[key] not in (None, ""):
            return str(tags[key])
        alt = key.replace("-", "_")
        if alt in tags and tags[alt] not in (None, ""):
            return str(tags[alt])
    return None


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

    m = _ACTION_RE.match(text)
    if m:
        body = (m.group(1) or "").strip()
        body = body.replace("\x01", "").replace("\u0001", "").strip()
        return body, True

    m = _ACTION_FALLBACK_RE.match(text)
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


def _strip_leading_reply_mention(content: str, tags: dict[str, Any]) -> str:
    original = (content or "").strip()
    if not original:
        return original

    parent_login = _tag_get(tags, "reply-parent-user-login")
    parent_display = _tag_get(tags, "reply-parent-display-name")
    names: list[str] = []
    for raw in (parent_login, parent_display):
        if not raw:
            continue
        name = _unescape_twitch_tag(raw).lstrip("@").strip()
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)

    for name in sorted(names, key=len, reverse=True):
        pat = re.compile(rf"^@{re.escape(name)}\b[:,]?\s+", re.IGNORECASE)
        cleaned = pat.sub("", original, count=1).strip()
        if cleaned and cleaned != original:
            return cleaned

    is_reply = bool(_tag_get(tags, "reply-parent-msg-id"))
    if is_reply and original.startswith("@"):
        cleaned = _LEADING_AT_RE.sub("", original, count=1).strip()
        if cleaned:
            return cleaned

    return original


def _reply_header_from_tags(tags: dict[str, Any]) -> Optional[str]:
    parent_id = _tag_get(tags, "reply-parent-msg-id")
    if not parent_id:
        return None

    display = (
        _tag_get(tags, "reply-parent-display-name")
        or _tag_get(tags, "reply-parent-user-login")
        or "someone"
    )
    display = _unescape_twitch_tag(display).lstrip("@")
    display = display.replace("*", "").replace("`", "").replace("_", "\u02cd")[:64]

    body = _tag_get(tags, "reply-parent-msg-body") or ""
    body = _unescape_twitch_tag(body).replace("\n", " ").strip()
    body, _ = _normalize_content(body)
    body = body.replace("*", "\u2217").replace("`", "'")
    if len(body) > 120:
        body = body[:117] + "…"

    if body:
        return f"-# ↩️ Replying to **{display}**: {body}"
    return f"-# ↩️ Replying to **{display}**"


def _discord_content_for_twitch(message: discord.Message) -> str:
    text = message.content or ""

    def user_repl(m: re.Match[str]) -> str:
        uid = int(m.group(1))
        if message.guild:
            member = message.guild.get_member(uid)
            if member:
                return f"@{member.display_name}"
        return "@user"

    def role_repl(m: re.Match[str]) -> str:
        rid = int(m.group(1))
        if message.guild:
            role = message.guild.get_role(rid)
            if role:
                return f"@{role.name}"
        return "@role"

    def channel_repl(m: re.Match[str]) -> str:
        cid = int(m.group(1))
        if message.guild:
            ch = message.guild.get_channel(cid)
            if ch is not None and hasattr(ch, "name"):
                return f"#{ch.name}"  # type: ignore[union-attr]
        return "#channel"

    text = _DISCORD_MENTION_RE.sub(user_repl, text)
    text = _DISCORD_ROLE_RE.sub(role_repl, text)
    text = _DISCORD_CHANNEL_RE.sub(channel_repl, text)
    text = _DISCORD_EMOJI_RE.sub(r":\1:", text)
    text = text.replace("\n", " ").strip()

    extras: list[str] = []
    for att in message.attachments:
        extras.append(att.url or att.filename or "[attachment]")
    for sticker in getattr(message, "stickers", []) or []:
        extras.append(f"[sticker:{getattr(sticker, 'name', 'sticker')}]")

    if extras:
        extra_txt = " ".join(extras)
        text = f"{text} {extra_txt}".strip() if text else extra_txt

    return text[:480].strip()


class TwitchMirrorBot(twitch_commands.Bot if twitch_commands else object):  # type: ignore[misc]
    def __init__(
        self,
        discord_bot: commands.Bot,
        discord_channel_id: int,
        store: Optional[TwitchMapStore] = None,
    ) -> None:
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

        self._store = store or TwitchMapStore(TWITCH_MIRROR_DB_PATH, MSG_CACHE_MAX)

        # In-memory cache (also persisted to SQLite)
        self._msg_map: OrderedDict[str, int] = OrderedDict()  # twitch → discord (inbound)
        self._login_msgs: dict[str, set[str]] = defaultdict(set)
        self._discord_to_inbound: OrderedDict[int, str] = OrderedDict()

        self._outbound_pending: Deque[int] = deque()
        self._discord_to_twitch: OrderedDict[int, str] = OrderedDict()  # discord → twitch (outbound)
        self._send_lock = asyncio.Lock()

        self._webhook: Optional[discord.Webhook] = None
        self._text_channel: Optional[discord.TextChannel] = None

        self._broadcaster_id: Optional[str] = None
        self._moderator_id: Optional[str] = None
        self._has_delete_scope: bool = False
        self._has_send_scope: bool = False
        self._helix_client_id: str = TWITCH_CLIENT_ID

    async def _load_persisted_map(self) -> None:
        try:
            await self._store.init()
            rows = await self._store.load_recent()
        except Exception as e:
            print(f"[TwitchMirror] Map DB load failed: {e}")
            return

        inbound = 0
        outbound = 0
        for row in rows:
            tid = str(row["twitch_id"])
            did = int(row["discord_id"])
            login = (row.get("login") or "").lower()
            direction = row.get("direction") or "inbound"

            if direction == "outbound":
                self._discord_to_twitch[did] = tid
                self._discord_to_twitch.move_to_end(did)
                outbound += 1
            else:
                self._msg_map[tid] = did
                self._msg_map.move_to_end(tid)
                self._discord_to_inbound[did] = tid
                self._discord_to_inbound.move_to_end(did)
                if login:
                    self._login_msgs[login].add(tid)
                inbound += 1

        if inbound or outbound:
            print(
                f"[TwitchMirror] Restored message map from DB: "
                f"inbound={inbound} outbound={outbound} path={self._store.path}"
            )

    async def _remember(self, twitch_id: str, discord_id: int, login: str) -> None:
        self._msg_map[twitch_id] = discord_id
        self._msg_map.move_to_end(twitch_id)
        self._login_msgs[login.lower()].add(twitch_id)
        self._discord_to_inbound[discord_id] = twitch_id
        self._discord_to_inbound.move_to_end(discord_id)
        while len(self._msg_map) > MSG_CACHE_MAX:
            old_tid, old_did = self._msg_map.popitem(last=False)
            for s in self._login_msgs.values():
                s.discard(old_tid)
            self._discord_to_inbound.pop(old_did, None)
        while len(self._discord_to_inbound) > MSG_CACHE_MAX:
            self._discord_to_inbound.popitem(last=False)
        try:
            await self._store.upsert(
                twitch_id=twitch_id,
                discord_id=discord_id,
                login=login,
                direction="inbound",
            )
        except Exception as e:
            print(f"[TwitchMirror] Map persist (inbound) failed: {e}")

    async def _forget_twitch_id(self, twitch_id: str) -> Optional[int]:
        discord_id = self._msg_map.pop(twitch_id, None)
        for s in self._login_msgs.values():
            s.discard(twitch_id)
        if discord_id is not None:
            self._discord_to_inbound.pop(discord_id, None)
        try:
            row = await self._store.delete_by_twitch(twitch_id)
            if discord_id is None and row is not None:
                discord_id = int(row["discord_id"])
        except Exception as e:
            print(f"[TwitchMirror] Map DB delete (twitch) failed: {e}")
        return discord_id

    async def _forget_inbound_by_discord(self, discord_id: int) -> Optional[str]:
        twitch_id = self._discord_to_inbound.pop(discord_id, None)
        if twitch_id is not None:
            self._msg_map.pop(twitch_id, None)
            for s in self._login_msgs.values():
                s.discard(twitch_id)
        try:
            row = await self._store.delete_by_discord(discord_id, direction="inbound")
            if twitch_id is None and row is not None:
                twitch_id = str(row["twitch_id"])
        except Exception as e:
            print(f"[TwitchMirror] Map DB delete (discord inbound) failed: {e}")
        return twitch_id

    async def _remember_outbound(self, discord_id: int, twitch_id: str) -> None:
        self._discord_to_twitch[discord_id] = twitch_id
        self._discord_to_twitch.move_to_end(discord_id)
        while len(self._discord_to_twitch) > MSG_CACHE_MAX:
            self._discord_to_twitch.popitem(last=False)
        try:
            await self._store.upsert(
                twitch_id=twitch_id,
                discord_id=discord_id,
                login=None,
                direction="outbound",
            )
        except Exception as e:
            print(f"[TwitchMirror] Map persist (outbound) failed: {e}")

    async def _forget_outbound(self, discord_id: int) -> Optional[str]:
        twitch_id = self._discord_to_twitch.pop(discord_id, None)
        try:
            row = await self._store.delete_by_discord(discord_id, direction="outbound")
            if twitch_id is None and row is not None:
                twitch_id = str(row["twitch_id"])
        except Exception as e:
            print(f"[TwitchMirror] Map DB delete (outbound) failed: {e}")
        return twitch_id

    def _helix_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {_bearer_token(TWITCH_TOKEN)}",
            "Client-Id": self._helix_client_id or TWITCH_CLIENT_ID,
            "Content-Type": "application/json",
        }

    async def _resolve_helix_ids(self) -> None:
        if aiohttp is None:
            print("[TwitchMirror] Helix unavailable (aiohttp missing)")
            return

        bearer = _bearer_token(TWITCH_TOKEN)
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://id.twitch.tv/oauth2/validate",
                    headers={"Authorization": f"OAuth {bearer}"},
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        print(
                            f"[TwitchMirror] Token validate HTTP {resp.status}: "
                            f"{body[:200]}"
                        )
                        return
                    info: dict[str, Any] = await resp.json()

                self._moderator_id = str(info.get("user_id") or "") or None
                scopes = list(info.get("scopes") or [])
                self._has_delete_scope = REQUIRED_DELETE_SCOPE in scopes
                self._has_send_scope = REQUIRED_SEND_SCOPE in scopes
                login = info.get("login") or "?"
                token_client_id = str(info.get("client_id") or "")

                if token_client_id:
                    if TWITCH_CLIENT_ID and TWITCH_CLIENT_ID != token_client_id:
                        print(
                            f"[TwitchMirror] WARNING: TWITCH_CLIENT_ID mismatch!\n"
                            f"  .env TWITCH_CLIENT_ID = {TWITCH_CLIENT_ID}\n"
                            f"  token client_id       = {token_client_id}\n"
                            f"  → using token's client_id for Helix calls"
                        )
                    self._helix_client_id = token_client_id
                elif TWITCH_CLIENT_ID:
                    self._helix_client_id = TWITCH_CLIENT_ID
                else:
                    print("[TwitchMirror] No Client-Id available")
                    return

                print(
                    f"[TwitchMirror] Token user={login} id={self._moderator_id} "
                    f"client_id={self._helix_client_id[:8]}… scopes={len(scopes)}"
                )
                if self._has_delete_scope:
                    print(f"[TwitchMirror] Scope OK: {REQUIRED_DELETE_SCOPE}")
                else:
                    print(
                        f"[TwitchMirror] WARNING: missing `{REQUIRED_DELETE_SCOPE}`"
                    )
                if self._has_send_scope:
                    print(f"[TwitchMirror] Scope OK: {REQUIRED_SEND_SCOPE}")
                else:
                    print(
                        f"[TwitchMirror] WARNING: missing `{REQUIRED_SEND_SCOPE}` "
                        f"— Discord→Twitch will use IRC (no reliable msg id)"
                    )

                headers = self._helix_headers()
                async with session.get(
                    f"https://api.twitch.tv/helix/users?login={TWITCH_CHANNEL}",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        print(
                            f"[TwitchMirror] Helix users (broadcaster) "
                            f"HTTP {resp.status}: {body[:200]}"
                        )
                        return
                    data = await resp.json()
                users = data.get("data") or []
                if not users:
                    print(f"[TwitchMirror] Broadcaster login not found: {TWITCH_CHANNEL}")
                    return
                self._broadcaster_id = str(users[0]["id"])
                print(
                    f"[TwitchMirror] Helix ready: broadcaster={self._broadcaster_id} "
                    f"sender/mod={self._moderator_id}"
                )
        except Exception as e:
            print(f"[TwitchMirror] Helix id resolve failed: {e}")

    async def event_ready(self) -> None:
        self.connected = True
        nick = getattr(self, "nick", None) or TWITCH_NICK or "?"
        print(f"[TwitchMirror] Connected as {nick} → #{TWITCH_CHANNEL}")
        if OWNER_PING_MAP:
            print(
                f"[TwitchMirror] Owner ping map (@ or bare): "
                + ", ".join(f"{k}→<@{v}>" for k, v in OWNER_PING_MAP.items())
            )
        print("[TwitchMirror] Moderation sync + reply @ strip")
        await self._load_persisted_map()
        await self._resolve_helix_ids()
        if DISCORD_TO_TWITCH:
            print("[TwitchMirror] Discord → Twitch: ON (Helix preferred)")
        else:
            print("[TwitchMirror] Discord → Twitch: OFF")
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._discord_worker())

    async def event_message(self, message) -> None:  # type: ignore[no-untyped-def]
        try:
            if getattr(message, "echo", False):
                tid = _twitch_msg_id(message)
                if tid and self._outbound_pending:
                    discord_id = self._outbound_pending.popleft()
                    await self._remember_outbound(discord_id, tid)
                    print(
                        f"[TwitchMirror] Outbound (IRC echo) linked "
                        f"discord={discord_id} → twitch={tid[:12]}…"
                    )
                return

            author = message.author
            login = (getattr(author, "name", None) or "unknown").lower()
            display = (
                getattr(author, "display_name", None)
                or getattr(author, "name", None)
                or "unknown"
            )
            raw = message.content or ""
            content, is_action = _normalize_content(raw)
            if not content:
                return

            tags = _message_tags(message)
            reply_header: Optional[str] = None
            try:
                reply_header = _reply_header_from_tags(tags)
                if reply_header:
                    before = content
                    content = _strip_leading_reply_mention(content, tags)
                    if content != before:
                        print(
                            f"[TwitchMirror] stripped reply @mention: "
                            f"{before[:40]!r} → {content[:40]!r}"
                        )
            except Exception as e:
                print(f"[TwitchMirror] reply format error (sending plain): {e}")
                reply_header = None

            if not content:
                return

            tid = _twitch_msg_id(message)
            if not tid:
                print(
                    f"[TwitchMirror] WARNING: no msg id from IRC for @{login}"
                )

            await self._queue.put(
                (
                    login,
                    display,
                    content[:1900],
                    tid,
                    reply_header,
                    is_action,
                )
            )
        except Exception as e:
            print(f"[TwitchMirror] event_message error: {e}")

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

    async def send_to_twitch(self, text: str, discord_msg_id: int) -> bool:
        text = (text or "").strip()
        if not text:
            return False

        async with self._send_lock:
            if (
                aiohttp is not None
                and self._helix_client_id
                and self._broadcaster_id
                and self._moderator_id
                and self._has_send_scope
            ):
                try:
                    url = "https://api.twitch.tv/helix/chat/messages"
                    payload = {
                        "broadcaster_id": self._broadcaster_id,
                        "sender_id": self._moderator_id,
                        "message": text[:500],
                    }
                    timeout = aiohttp.ClientTimeout(total=10)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(
                            url, json=payload, headers=self._helix_headers()
                        ) as resp:
                            body_txt = await resp.text()
                            if resp.status in (200, 201):
                                try:
                                    data = await resp.json(content_type=None)
                                except Exception:
                                    import json as _json

                                    data = _json.loads(body_txt)
                                rows = data.get("data") or []
                                mid = None
                                if rows:
                                    mid = rows[0].get("message_id")
                                    is_sent = rows[0].get("is_sent", True)
                                    if not is_sent:
                                        drop = rows[0].get("drop_reason")
                                        print(
                                            f"[TwitchMirror] Helix send dropped: {drop}"
                                        )
                                        return False
                                if mid:
                                    await self._remember_outbound(discord_msg_id, str(mid))
                                    print(
                                        f"[TwitchMirror] Helix send OK "
                                        f"discord={discord_msg_id} → "
                                        f"twitch={str(mid)[:12]}…"
                                    )
                                else:
                                    print(
                                        "[TwitchMirror] Helix send OK but no message_id "
                                        f"in response: {body_txt[:200]}"
                                    )
                                await asyncio.sleep(SEND_DELAY)
                                return True
                            print(
                                f"[TwitchMirror] Helix send HTTP {resp.status}: "
                                f"{body_txt[:250]}"
                            )
                except Exception as e:
                    print(f"[TwitchMirror] Helix send error: {e}")

            if not self.connected:
                print("[TwitchMirror] send_to_twitch: not connected")
                return False
            try:
                channel = self.get_channel(TWITCH_CHANNEL)
                if channel is None:
                    channel = self.get_channel(f"#{TWITCH_CHANNEL}")
                if channel is None:
                    print(f"[TwitchMirror] No IRC channel for #{TWITCH_CHANNEL}")
                    return False

                self._outbound_pending.append(discord_msg_id)
                await channel.send(text[:480])
                await asyncio.sleep(SEND_DELAY)
                print(
                    "[TwitchMirror] IRC send used (no Helix msg id — "
                    "delete-from-Discord may not work for this message)"
                )
                return True
            except Exception as e:
                try:
                    if self._outbound_pending and self._outbound_pending[-1] == discord_msg_id:
                        self._outbound_pending.pop()
                except Exception:
                    pass
                print(f"[TwitchMirror] IRC send failed: {e}")
                return False

    async def delete_on_twitch(self, twitch_msg_id: str) -> bool:
        if not twitch_msg_id:
            return False

        if aiohttp is None:
            print("[TwitchMirror] delete failed: aiohttp missing")
            return False

        if not self._helix_client_id:
            print("[TwitchMirror] delete failed: no Client-Id")
            return False

        if not self._broadcaster_id or not self._moderator_id:
            print("[TwitchMirror] delete failed: broadcaster/moderator id unknown")
            return False

        if not self._has_delete_scope:
            print(
                f"[TwitchMirror] delete failed: missing scope `{REQUIRED_DELETE_SCOPE}`"
            )
            return False

        try:
            url = "https://api.twitch.tv/helix/moderation/chat"
            params = {
                "broadcaster_id": self._broadcaster_id,
                "moderator_id": self._moderator_id,
                "message_id": twitch_msg_id,
            }
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.delete(
                    url, params=params, headers=self._helix_headers()
                ) as resp:
                    if resp.status in (200, 204):
                        print(
                            f"[TwitchMirror] Helix delete OK {twitch_msg_id[:12]}… "
                            f"HTTP {resp.status}"
                        )
                        return True
                    body = await resp.text()
                    print(
                        f"[TwitchMirror] Helix delete FAILED HTTP {resp.status}\n"
                        f"  msg_id={twitch_msg_id}\n"
                        f"  broadcaster={self._broadcaster_id} "
                        f"moderator={self._moderator_id}\n"
                        f"  body={body[:300]}"
                    )
                    return False
        except Exception as e:
            print(f"[TwitchMirror] Helix delete error: {e}")
            return False

    async def _fetch_avatar(self, login: str) -> Optional[str]:
        login = login.lower()
        if login in self._avatar_cache:
            return self._avatar_cache[login]

        if not self._helix_client_id or aiohttp is None:
            self._avatar_cache[login] = None
            return None

        async with self._avatar_lock:
            if login in self._avatar_cache:
                return self._avatar_cache[login]

            url = f"https://api.twitch.tv/helix/users?login={login}"
            try:
                timeout = aiohttp.ClientTimeout(total=8)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=self._helix_headers()) as resp:
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
        self._discord_to_inbound.pop(discord_msg_id, None)

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
            discord_id = await self._forget_twitch_id(value)
            if discord_id is not None:
                await self._delete_discord_message(discord_id)
                print(f"[TwitchMirror] Deleted mirrored msg (CLEARMSG {value[:8]}…)")
            return

        if kind == "user" and value:
            login = value.lower()
            tids = set(self._login_msgs.get(login, set()))
            try:
                db_rows = await self._store.delete_by_login(login)
                for row in db_rows:
                    tids.add(str(row["twitch_id"]))
            except Exception as e:
                print(f"[TwitchMirror] Map DB delete_by_login failed: {e}")

            deleted = 0
            for tid in list(tids):
                discord_id = await self._forget_twitch_id(tid)
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
            self._discord_to_inbound.clear()
            try:
                db_rows = await self._store.clear_direction("inbound")
                seen = {tid for tid, _ in items}
                for row in db_rows:
                    tid = str(row["twitch_id"])
                    if tid not in seen:
                        items.append((tid, int(row["discord_id"])))
            except Exception as e:
                print(f"[TwitchMirror] Map DB clear inbound failed: {e}")

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
        if reply_header and body.startswith("@"):
            stripped = _LEADING_AT_RE.sub("", body, count=1).strip()
            if stripped:
                body = stripped
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

                if reply_header and content.startswith("@"):
                    stripped = _LEADING_AT_RE.sub("", content, count=1).strip()
                    if stripped:
                        content = stripped

                content, ping_ids = _apply_owner_pings(content)
                final = self._compose_content(content, reply_header, is_action)
                if not final.strip():
                    continue
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
                        if reply_header:
                            try:
                                sent = await webhook.send(
                                    content=content,
                                    username=username,
                                    avatar_url=avatar_url or discord.utils.MISSING,
                                    wait=True,
                                    allowed_mentions=mentions,
                                )
                                discord_msg_id = sent.id
                            except Exception as e2:
                                print(f"[TwitchMirror] Plain retry failed: {e2}")
                        else:
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
                    await self._remember(twitch_id, discord_msg_id, login)

                await asyncio.sleep(SEND_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TwitchMirror] Worker error: {e}")
                await asyncio.sleep(1.0)


class TwitchMirrorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._twitch: Optional[TwitchMirrorBot] = None
        self._task: Optional[asyncio.Task] = None
        self._store = TwitchMapStore(TWITCH_MIRROR_DB_PATH, MSG_CACHE_MAX)
        try:
            self._discord_channel_id = int(TWITCH_DISCORD_CHANNEL_ID) if TWITCH_DISCORD_CHANNEL_ID else 0
        except ValueError:
            self._discord_channel_id = 0

    async def cog_load(self) -> None:
        if twitch_commands is None:
            print("[TwitchMirror] twitchio not installed – cog idle")
            return
        if not _configured():
            print(
                "[TwitchMirror] Disabled – set TWITCH_TOKEN, TWITCH_CHANNEL, "
                "TWITCH_DISCORD_CHANNEL_ID in .env"
            )
            return

        try:
            channel_id = int(TWITCH_DISCORD_CHANNEL_ID)
        except ValueError:
            print(f"[TwitchMirror] Invalid TWITCH_DISCORD_CHANNEL_ID: {TWITCH_DISCORD_CHANNEL_ID!r}")
            return

        try:
            await self._store.init()
            print(f"[TwitchMirror] Map DB: {self._store.path}")
        except Exception as e:
            print(f"[TwitchMirror] Map DB init failed: {e}")

        self._discord_channel_id = channel_id
        self._twitch = TwitchMirrorBot(self.bot, channel_id, store=self._store)
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

    def _is_mirror_channel(self, channel_id: Optional[int]) -> bool:
        return bool(self._discord_channel_id and channel_id == self._discord_channel_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not DISCORD_TO_TWITCH:
            return
        if not self._is_mirror_channel(message.channel.id if message.channel else None):
            return
        if self._twitch is None or not self._twitch.connected:
            return

        if message.author.bot:
            return
        if message.webhook_id is not None:
            return
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return

        body = _discord_content_for_twitch(message)
        if not body:
            return

        display = (message.author.display_name or message.author.name or "Discord").strip()
        display = display.replace("\n", " ")[:32]
        payload = f"[Discord] {display}: {body}"
        if len(payload) > 480:
            payload = payload[:477] + "…"

        ok = await self._twitch.send_to_twitch(payload, message.id)
        if ok:
            print(f"[TwitchMirror] Discord→Twitch: {display}: {body[:60]!r}")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if not self._is_mirror_channel(payload.channel_id):
            return
        if self._twitch is None or not self._twitch.connected:
            return

        twitch_id = await self._twitch._forget_inbound_by_discord(payload.message_id)
        source = "inbound (Twitch original)"

        if not twitch_id and DISCORD_TO_TWITCH:
            twitch_id = await self._twitch._forget_outbound(payload.message_id)
            source = "outbound (Discord→Twitch)"

        if not twitch_id:
            print(
                f"[TwitchMirror] Discord delete msg={payload.message_id} "
                f"not in map (inbound={len(self._twitch._discord_to_inbound)} "
                f"outbound={len(self._twitch._discord_to_twitch)}) — skip Twitch delete"
            )
            return

        ok = await self._twitch.delete_on_twitch(twitch_id)
        if ok:
            print(
                f"[TwitchMirror] Discord delete → Twitch delete "
                f"{twitch_id[:12]}… ({source})"
            )
        else:
            print(
                f"[TwitchMirror] Could not delete on Twitch "
                f"({twitch_id[:12]}…, {source}). See Helix error above."
            )

    @app_commands.command(
        name="twitch-mirror-status",
        description="Shows Twitch chat mirror status",
    )
    @app_commands.default_permissions(administrator=True)
    async def twitch_mirror_status(self, interaction: discord.Interaction) -> None:
        if not _configured():
            await interaction.response.send_message(
                "Twitch mirror is **not configured**.",
                ephemeral=True,
            )
            return

        connected = bool(self._twitch and self._twitch.connected)
        tracked = len(self._twitch._msg_map) if self._twitch else 0
        outbound = len(self._twitch._discord_to_twitch) if self._twitch else 0
        has_del = bool(self._twitch and self._twitch._has_delete_scope)
        has_send = bool(self._twitch and self._twitch._has_send_scope)
        helix_ready = bool(
            self._twitch
            and self._twitch._broadcaster_id
            and self._twitch._moderator_id
            and self._twitch._helix_client_id
        )

        db_counts = {"total": 0, "inbound": 0, "outbound": 0}
        try:
            db_counts = await self._store.count()
        except Exception:
            pass

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
        embed.add_field(
            name="In memory",
            value=f"inbound `{tracked}` · outbound `{outbound}`",
            inline=True,
        )
        embed.add_field(
            name="Persisted (DB)",
            value=(
                f"total `{db_counts['total']}` · "
                f"in `{db_counts['inbound']}` · out `{db_counts['outbound']}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Discord → Twitch",
            value=(
                f"ON · send={'Helix' if has_send else 'IRC'}"
                if DISCORD_TO_TWITCH
                else "OFF"
            ),
            inline=True,
        )
        embed.add_field(
            name="Helix",
            value=(
                f"ids={'yes' if helix_ready else 'no'} · "
                f"delete={'yes' if has_del else 'NO'} · "
                f"send={'yes' if has_send else 'NO'}"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Map DB: {self._store.path} · max {MSG_CACHE_MAX}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TwitchMirrorCog(bot))
