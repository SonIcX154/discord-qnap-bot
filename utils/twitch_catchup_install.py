"""Wire robotty catch-up onto TwitchMirrorBot / TwitchMirrorCog.

Prefer calling ``wire_catchup()`` from ``cogs.twitch_mirror.setup``.
Kept as a module so the mixin stays testable without loading discord.
"""
from __future__ import annotations

import time
import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger("qnapbot.twitch_catchup")

_wired = False


def wire_catchup() -> None:
    """Attach catch-up mixin methods and reconnect hooks (idempotent)."""
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
    )

    TwitchMirrorBot = tm.TwitchMirrorBot
    TwitchMirrorCog = tm.TwitchMirrorCog

    # Copy mixin methods onto the bot class
    for name in (
        "_init_catchup_state",
        "_catchup_from_robotty",
        "_periodic_catchup_loop",
        "_start_catchup_tasks",
        "_cancel_catchup_tasks",
    ):
        setattr(TwitchMirrorBot, name, getattr(TwitchCatchupMixin, name))

    # --- init: catch-up state -------------------------------------------------
    orig_bot_init = TwitchMirrorBot.__init__

    def bot_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_bot_init(self, *args, **kwargs)
        self._init_catchup_state()

    TwitchMirrorBot.__init__ = bot_init  # type: ignore[method-assign]

    # --- event_ready: start catch-up ------------------------------------------
    orig_ready = TwitchMirrorBot.event_ready

    async def event_ready(self: Any) -> None:
        await orig_ready(self)
        self._start_catchup_tasks()

    TwitchMirrorBot.event_ready = event_ready  # type: ignore[method-assign]

    # --- shutdown: cancel tasks -----------------------------------------------
    orig_shutdown = TwitchMirrorCog._shutdown_client

    async def _shutdown_client(self: Any, client: Any) -> None:
        if client is not None and hasattr(client, "_cancel_catchup_tasks"):
            await client._cancel_catchup_tasks()
        await orig_shutdown(self, client)

    TwitchMirrorCog._shutdown_client = _shutdown_client  # type: ignore[method-assign]

    # --- supervisor: pending window across reconnects -------------------------
    async def _run_twitch(self: Any) -> None:
        if not hasattr(self, "_pending_catchup_ts"):
            self._pending_catchup_ts = None

        backoff = 5.0
        while True:
            client = None
            start_task = None
            watch_task = None
            was_ready = False
            try:
                catchup_ts = self._pending_catchup_ts
                self._pending_catchup_ts = None
                client = tm.TwitchMirrorBot(
                    self.bot, self._discord_channel_id, store=self._store
                )
                client._catchup_after_ts = catchup_ts
                self._twitch = client
                start_task = asyncio.create_task(
                    client.start(), name="twitch-mirror-start"
                )
                watch_task = asyncio.create_task(
                    self._irc_watchdog(client, start_task),
                    name="twitch-mirror-watchdog",
                )

                done, pending = await asyncio.wait(
                    {start_task, watch_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

                was_ready = bool(getattr(client, "_was_ready", False))

                if start_task in done and not start_task.cancelled():
                    exc = start_task.exception()
                    if exc is not None:
                        log.error(
                            "Twitch client crashed: %s – retry in %.0fs",
                            exc,
                            backoff,
                        )
                    else:
                        log.warning(
                            "Twitch client exited – reconnecting in %.0fs",
                            backoff,
                        )
                else:
                    log.warning(
                        "Twitch reconnect scheduled in %.0fs (watchdog or cancel)",
                        backoff,
                    )

            except asyncio.CancelledError:
                await self._shutdown_client(client)
                self._twitch = None
                break
            except Exception as e:
                log.error("Twitch run loop error: %s – retry in %.0fs", e, backoff)
            finally:
                if client is not None:
                    if getattr(client, "_was_ready", False) and CATCHUP_ENABLED:
                        self._pending_catchup_ts = float(client._last_irc_activity)
                        log.info(
                            "Catch-up window starts at %s",
                            time.strftime(
                                "%H:%M:%S",
                                time.localtime(self._pending_catchup_ts),
                            ),
                        )
                    await self._shutdown_client(client)
                if self._twitch is client:
                    self._twitch = None

            await asyncio.sleep(backoff)
            if was_ready:
                backoff = 5.0
            else:
                backoff = min(backoff * 1.5, 120.0)

    TwitchMirrorCog._run_twitch = _run_twitch  # type: ignore[method-assign]

    orig_cog_init = TwitchMirrorCog.__init__

    def cog_init(self: Any, bot: Any) -> None:
        orig_cog_init(self, bot)
        self._pending_catchup_ts = None

    TwitchMirrorCog.__init__ = cog_init  # type: ignore[method-assign]

    _wired = True
    log.info(
        "Catch-up wired: enabled=%s limit=%s interval=%ss api=%s",
        CATCHUP_ENABLED,
        CATCHUP_LIMIT,
        CATCHUP_INTERVAL,
        CATCHUP_API,
    )


# Backwards-compatible alias
install_catchup = wire_catchup
