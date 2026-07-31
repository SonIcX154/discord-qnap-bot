"""Pure helpers + env config for the Twitch ↔ Discord mirror."""
from __future__ import annotations

import os
import re
from typing import Any, Optional
from urllib.parse import unquote

import discord

try:
    from utils.twitch_map_store import DEFAULT_PATH as TWITCH_MAP_DEFAULT_PATH
except ImportError:  # pragma: no cover
    from .twitch_map_store import DEFAULT_PATH as TWITCH_MAP_DEFAULT_PATH

# ---------------------------------------------------------------------------
# Env / config
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------
CLEARMSG_RE = re.compile(
    r"target-msg-id=([^;\s]+).*\sCLEARMSG\s",
    re.IGNORECASE,
)
CLEARCHAT_USER_RE = re.compile(
    r"CLEARCHAT\s+#\S+\s+:(\S+)",
    re.IGNORECASE,
)
ACTION_RE = re.compile(r"^\x01ACTION[ ]?(.*)\x01$", re.DOTALL | re.IGNORECASE)
ACTION_FALLBACK_RE = re.compile(
    r"^(?:\x01|\u0001)ACTION[ ]?(.*?)(?:\x01|\u0001)$",
    re.DOTALL | re.IGNORECASE,
)
LEADING_AT_RE = re.compile(r"^@\S+\s+")
DISCORD_MENTION_RE = re.compile(r"<@!?(\d+)>")
DISCORD_ROLE_RE = re.compile(r"<@&(\d+)>")
DISCORD_CHANNEL_RE = re.compile(r"<#(\d+)>")
DISCORD_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")


def is_configured() -> bool:
    return bool(TWITCH_TOKEN and TWITCH_CHANNEL and TWITCH_DISCORD_CHANNEL_ID)


def normalize_token(token: str) -> str:
    t = token.strip()
    if t.lower().startswith("oauth:"):
        return t
    return f"oauth:{t}"


def bearer_token(token: str) -> str:
    t = token.strip()
    if t.lower().startswith("oauth:"):
        return t[6:]
    return t


def safe_webhook_username(name: str) -> str:
    base = (name or "Twitch User").strip() or "Twitch User"
    if base.lower() == "clyde":
        base = "Clyde_"
    return base[:80]


def build_owner_ping_map() -> dict[str, int]:
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


OWNER_PING_MAP = build_owner_ping_map()

_OWNER_MENTION_RE: Optional[re.Pattern[str]] = None
if OWNER_PING_MAP:
    names = sorted(OWNER_PING_MAP.keys(), key=len, reverse=True)
    alternation = "|".join(re.escape(n) for n in names)
    _OWNER_MENTION_RE = re.compile(rf"(?i)(?<!\w)@?({alternation})(?!\w)")


def apply_owner_pings(content: str) -> tuple[str, list[int]]:
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


def allowed_mentions_for(user_ids: list[int]) -> discord.AllowedMentions:
    if not user_ids:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=uid) for uid in user_ids],
    )


def twitch_msg_id(message: Any) -> Optional[str]:
    mid = getattr(message, "id", None)
    if mid:
        return str(mid)
    tags = getattr(message, "tags", None) or {}
    if isinstance(tags, dict):
        for key in ("id", "msg-id"):
            if tags.get(key):
                return str(tags[key])
    return None


def message_tags(message: Any) -> dict[str, Any]:
    tags = getattr(message, "tags", None)
    if isinstance(tags, dict):
        return tags
    if tags is not None and hasattr(tags, "items"):
        try:
            return dict(tags.items())  # type: ignore[arg-type]
        except Exception:
            pass
    return {}


def tag_get(tags: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        if key in tags and tags[key] not in (None, ""):
            return str(tags[key])
        alt = key.replace("-", "_")
        if alt in tags and tags[alt] not in (None, ""):
            return str(tags[alt])
    return None


def unescape_twitch_tag(value: str) -> str:
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


def normalize_content(raw: str) -> tuple[str, bool]:
    text = (raw or "").strip()
    if not text:
        return "", False

    m = ACTION_RE.match(text)
    if m:
        body = (m.group(1) or "").strip()
        body = body.replace("\x01", "").replace("\u0001", "").strip()
        return body, True

    m = ACTION_FALLBACK_RE.match(text)
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


def strip_leading_reply_mention(content: str, tags: dict[str, Any]) -> str:
    original = (content or "").strip()
    if not original:
        return original

    parent_login = tag_get(tags, "reply-parent-user-login")
    parent_display = tag_get(tags, "reply-parent-display-name")
    names: list[str] = []
    for raw in (parent_login, parent_display):
        if not raw:
            continue
        name = unescape_twitch_tag(raw).lstrip("@").strip()
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)

    for name in sorted(names, key=len, reverse=True):
        pat = re.compile(rf"^@{re.escape(name)}\b[:,]?\s+", re.IGNORECASE)
        cleaned = pat.sub("", original, count=1).strip()
        if cleaned and cleaned != original:
            return cleaned

    is_reply = bool(tag_get(tags, "reply-parent-msg-id"))
    if is_reply and original.startswith("@"):
        cleaned = LEADING_AT_RE.sub("", original, count=1).strip()
        if cleaned:
            return cleaned

    return original


def reply_header_from_tags(tags: dict[str, Any]) -> Optional[str]:
    parent_id = tag_get(tags, "reply-parent-msg-id")
    if not parent_id:
        return None

    display = (
        tag_get(tags, "reply-parent-display-name")
        or tag_get(tags, "reply-parent-user-login")
        or "someone"
    )
    display = unescape_twitch_tag(display).lstrip("@")
    display = display.replace("*", "").replace("`", "").replace("_", "\u02cd")[:64]

    body = tag_get(tags, "reply-parent-msg-body") or ""
    body = unescape_twitch_tag(body).replace("\n", " ").strip()
    body, _ = normalize_content(body)
    body = body.replace("*", "\u2217").replace("`", "'")
    if len(body) > 120:
        body = body[:117] + "…"

    if body:
        return f"-# ↩️ Replying to **{display}**: {body}"
    return f"-# ↩️ Replying to **{display}**"


def discord_content_for_twitch(message: discord.Message) -> str:
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

    text = DISCORD_MENTION_RE.sub(user_repl, text)
    text = DISCORD_ROLE_RE.sub(role_repl, text)
    text = DISCORD_CHANNEL_RE.sub(channel_repl, text)
    text = DISCORD_EMOJI_RE.sub(r":\1:", text)
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


def compose_mirror_content(
    content: str,
    reply_header: Optional[str],
    is_action: bool = False,
) -> str:
    body = content
    if reply_header and body.startswith("@"):
        stripped = LEADING_AT_RE.sub("", body, count=1).strip()
        if stripped:
            body = stripped
    if reply_header:
        return f"{reply_header}\n{body}"
    return body
