"""Fetch missed Twitch chat lines via recent-messages.robotty.de (Chatterino-style)."""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("qnapbot.twitch_catchup")

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore

DEFAULT_API = "https://recent-messages.robotty.de/api/v2/recent-messages"
USER_AGENT = "discord-qnap-bot/twitch-mirror"

# @tags :nick!user@host PRIVMSG #channel :body
_PRIVMSG_RE = re.compile(
    r"^(?:@(?P<tags>[^ ]+) )?(?::(?P<nick>[^!]+)![^ ]+ )?PRIVMSG #(?P<channel>\S+) :(?P<body>.*)$",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class CatchupMessage:
    login: str
    display: str
    content: str
    twitch_id: str
    sent_ts_ms: int
    is_action: bool = False

    @property
    def sent_dt(self) -> datetime:
        return datetime.fromtimestamp(self.sent_ts_ms / 1000.0, tz=timezone.utc)

    def time_label(self, tz: Optional[timezone] = None) -> str:
        dt = self.sent_dt
        if tz is not None:
            dt = dt.astimezone(tz)
        else:
            dt = dt.astimezone()
        return dt.strftime("%H:%M")


def parse_irc_tags(raw: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    if not raw:
        return tags
    for part in raw.split(";"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            tags[k] = v
        else:
            tags[part] = ""
    return tags


def parse_privmsg_line(line: str) -> Optional[CatchupMessage]:
    """Parse one raw IRC line; return None if not a usable PRIVMSG."""
    text = (line or "").strip()
    if not text or "PRIVMSG" not in text.upper():
        return None

    m = _PRIVMSG_RE.match(text)
    if not m:
        return None

    tags = parse_irc_tags(m.group("tags") or "")
    body = m.group("body") or ""
    nick = (m.group("nick") or tags.get("login") or "unknown").lower()
    display = tags.get("display-name") or nick
    msg_id = tags.get("id") or ""
    ts_raw = tags.get("tmi-sent-ts") or tags.get("rm-received-ts") or ""

    if not msg_id or not ts_raw:
        return None
    try:
        ts_ms = int(ts_raw)
    except ValueError:
        return None

    content = body
    is_action = False
    # /me ACTION
    if content.startswith("\x01ACTION") and content.endswith("\x01"):
        is_action = True
        content = content[7:-1].strip()
    elif content.startswith("\x01ACTION ") and content.endswith("\x01"):
        is_action = True
        content = content[8:-1].strip()

    content = content.strip()
    if not content:
        return None

    return CatchupMessage(
        login=nick,
        display=display,
        content=content[:1900],
        twitch_id=str(msg_id),
        sent_ts_ms=ts_ms,
        is_action=is_action,
    )


async def fetch_recent_messages(
    channel: str,
    *,
    after_ts: float,
    limit: int = 100,
    api_base: str = DEFAULT_API,
) -> list[CatchupMessage]:
    """Load recent PRIVMSGs for channel with tmi-sent-ts > after_ts (unix seconds)."""
    if aiohttp is None:
        raise RuntimeError("aiohttp not installed")

    channel = channel.strip().lstrip("#").lower()
    if not channel:
        return []

    after_ms = int(after_ts * 1000)
    url = f"{api_base.rstrip('/')}/{channel}"
    params = {"limit": str(max(1, min(int(limit), 800)))}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params=params) as resp:
            body_txt = await resp.text()
            if resp.status == 403:
                log.warning("robotty 403 for #%s (excluded/opt-out)", channel)
                return []
            if resp.status != 200:
                log.warning("robotty HTTP %s for #%s: %s", resp.status, channel, body_txt[:200])
                return []
            try:
                data: dict[str, Any] = await resp.json(content_type=None)
            except Exception:
                import json as _json

                data = _json.loads(body_txt)

    if data.get("error"):
        log.warning("robotty error for #%s: %s", channel, data.get("error"))

    raw_lines = data.get("messages") or []
    if not isinstance(raw_lines, list):
        return []

    out: list[CatchupMessage] = []
    for line in raw_lines:
        if not isinstance(line, str):
            continue
        msg = parse_privmsg_line(line)
        if msg is None:
            continue
        if msg.sent_ts_ms <= after_ms:
            continue
        out.append(msg)

    out.sort(key=lambda m: (m.sent_ts_ms, m.twitch_id))
    return out
