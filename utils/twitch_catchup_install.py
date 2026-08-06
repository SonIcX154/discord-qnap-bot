"""Attach TwitchCatchupMixin + reconnect supervisor to Twitch mirror classes.

This is intentional thin wiring (not business logic). Catch-up methods live in
``utils.twitch_catchup_mixin``; robotty I/O in ``utils.twitch_catchup``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("qnapbot.twitch_catchup")

_wired = False


def wire_catchup() -> None:
    """Make TwitchMirrorBot inherit the mixin and use the catch-up supervisor."""
    global _wired
    if _wired:
        return

    try:
        from cogs import twitch_mirror as tm
    except ImportError:
        from . import twitch_mirror as tm  # type: ignore

    from utils.twitch_catchup_mixin import (
        TwitchCatchupMixin,
        CATCHUP_ENABLED,
        CATCHUP_LIMIT,
        CATCHUP_INTERVAL,
        CATCHUP_API,
        run_twitch_supervisor,
    )

    Bot = tm.TwitchMirrorBot
    Cog = tm.TwitchMirrorCog

    # --- inheritance (native methods on the instance) -------------------------
    if TwitchCatchupMixin not in Bot.__bases__:
        Bot.__bases__ = (TwitchCatchupMixin,) + Bot.__bases__

    orig_bot_init = Bot.__init__

    def bot_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_bot_init(self, *args, **kwargs)
        self._init_catchup_state()

    Bot.__init__ = bot_init  # type: ignore[method-assign]

    orig_ready = Bot.event_ready

    async def event_ready(self: Any) -> None:
        await orig_ready(self)
        self._start_catchup_tasks()

    Bot.event_ready = event_ready  # type: ignore[method-assign]

    orig_shutdown = Cog._shutdown_client

    async def _shutdown_client(self: Any, client: Any) -> None:
        if client is not None:
            await client._cancel_catchup_tasks()
        await orig_shutdown(self, client)

    Cog._shutdown_client = _shutdown_client  # type: ignore[method-assign]

    # Supervisor with catch-up window across reconnects
    async def _run_twitch(self: Any) -> None:
        await run_twitch_supervisor(self)

    Cog._run_twitch = _run_twitch  # type: ignore[method-assign]

    orig_cog_init = Cog.__init__

    def cog_init(self: Any, bot: Any) -> None:
        orig_cog_init(self, bot)
        self._pending_catchup_ts = None

    Cog.__init__ = cog_init  # type: ignore[method-assign]

    _wired = True
    log.info(
        "Catch-up wired (mixin): enabled=%s limit=%s interval=%ss api=%s",
        CATCHUP_ENABLED,
        CATCHUP_LIMIT,
        CATCHUP_INTERVAL,
        CATCHUP_API,
    )


install_catchup = wire_catchup
