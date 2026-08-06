from __future__ import annotations

import os
import re
import time
import asyncio
import logging
import discord
from discord.ext import commands
from typing import Optional, Any, Deque
from collections import OrderedDict, defaultdict, deque

log = logging.getLogger("qnapbot.twitch_mirror")

try:
    from twitchio.ext import commands as twitch_commands
except ImportError:
    twitch_commands = None  # type: ignore

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore

try:
    from utils.twitch_map_store import TwitchMapStore
    from utils.twitch_catchup_mixin import (
        TwitchCatchupMixin,
        CATCHUP_ENABLED,
        run_twitch_supervisor,
    )
    from utils.twitch_helpers import (
        TWITCH_TOKEN,
        TWITCH_CHANNEL,
        TWITCH_DISCORD_CHANNEL_ID,
        TWITCH_NICK,
        TWITCH_CLIENT_ID,
        DISCORD_TO_TWITCH,
        SEND_DELAY,
        MSG_CACHE_MAX,
        CLEAR_WINDOW_SECONDS,
        TWITCH_MIRROR_DB_PATH,
        WEBHOOK_NAME,
        REQUIRED_DELETE_SCOPE,
        REQUIRED_SEND_SCOPE,
        CLEARMSG_RE,
        CLEARCHAT_USER_RE,
        LEADING_AT_RE,
        OWNER_PING_MAP,
        is_configured,
        normalize_token,
        bearer_token,
        safe_webhook_username,
        apply_owner_pings,
        allowed_mentions_for,
        twitch_msg_id,
        message_tags,
        normalize_content,
        strip_leading_reply_mention,
        reply_header_from_tags,
        discord_content_for_twitch,
        compose_mirror_content,
    )
except ImportError:
    from ..utils.twitch_map_store import TwitchMapStore
    from ..utils.twitch_catchup_mixin import (
        TwitchCatchupMixin,
        CATCHUP_ENABLED,
        run_twitch_supervisor,
    )
    from ..utils.twitch_helpers import (
        TWITCH_TOKEN,
        TWITCH_CHANNEL,
        TWITCH_DISCORD_CHANNEL_ID,
        TWITCH_NICK,
        TWITCH_CLIENT_ID,
        DISCORD_TO_TWITCH,
        SEND_DELAY,
        MSG_CACHE_MAX,
        CLEAR_WINDOW_SECONDS,
        TWITCH_MIRROR_DB_PATH,
        WEBHOOK_NAME,
        REQUIRED_DELETE_SCOPE,
        REQUIRED_SEND_SCOPE,
        CLEARMSG_RE,
        CLEARCHAT_USER_RE,
        LEADING_AT_RE,
        OWNER_PING_MAP,
        is_configured,
        normalize_token,
        bearer_token,
        safe_webhook_username,
        apply_owner_pings,
        allowed_mentions_for,
        twitch_msg_id,
        message_tags,
        normalize_content,
        strip_leading_reply_mention,
        reply_header_from_tags,
        discord_content_for_twitch,
        compose_mirror_content,
    )

# No IRC traffic (incl. server PINGs) for this long → treat as blackhole and reconnect.
# Twitch typically PINGs every ~4 minutes; default 5 min is slightly above that.
IRC_IDLE_SECONDS = max(120, int(os.getenv("TWITCH_IRC_IDLE_SECONDS", "300")))
IRC_WATCHDOG_INTERVAL = 30.0


_TwitchBase = twitch_commands.Bot if twitch_commands else object


class TwitchMirrorBot(TwitchCatchupMixin, _TwitchBase):  # type: ignore[misc]
    def __init__(
        self,
        discord_bot: commands.Bot,
        discord_channel_id: int,
        store: Optional[TwitchMapStore] = None,
    ) -> None:
        if twitch_commands is None:
            raise RuntimeError("twitchio is not installed")

        super().__init__(
            token=normalize_token(TWITCH_TOKEN),
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
        self._was_ready = False
        self._last_irc_activity = time.time()
        self._avatar_cache: dict[str, Optional[str]] = {}
        self._avatar_lock = asyncio.Lock()

        self._store = store or TwitchMapStore(TWITCH_MIRROR_DB_PATH, MSG_CACHE_MAX)

        self._msg_map: OrderedDict[str, int] = OrderedDict()
        self._login_msgs: dict[str, set[str]] = defaultdict(set)
        self._discord_to_inbound: OrderedDict[int, str] = OrderedDict()

        self._outbound_pending: Deque[int] = deque()
        self._discord_to_twitch: OrderedDict[int, str] = OrderedDict()
        self._outbound_twitch_ids: OrderedDict[str, None] = OrderedDict()
        self._bot_logins: set[str] = set()
        if TWITCH_NICK:
            self._bot_logins.add(TWITCH_NICK.lower().lstrip("#"))
        self._send_lock = asyncio.Lock()

        self._webhook: Optional[discord.Webhook] = None
        self._text_channel: Optional[discord.TextChannel] = None

        self._broadcaster_id: Optional[str] = None
        self._moderator_id: Optional[str] = None
        self._has_delete_scope: bool = False
        self._has_send_scope: bool = False
        self._helix_client_id: str = TWITCH_CLIENT_ID

        self._init_catchup_state()
