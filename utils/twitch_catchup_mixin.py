"""Robotty catch-up mixed into TwitchMirrorBot + reconnect supervisor."""
from __future__ import annotations

import os
import time
import asyncio
import logging
from typing import Any, Optional

import discord

log = logging.getLogger("qnapbot.twitch_catchup")

CATCHUP_ENABLED = os.getenv("TWITCH_CATCHUP", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
CATCHUP_LIMIT = max(10, min(500, int(os.getenv("TWITCH_CATCHUP_LIMIT", "50"))))
CATCHUP_INTERVAL = max(0, int(os.getenv("TWITCH_CATCHUP_INTERVAL", "300")))  # 0 = off
CATCHUP_API = os.getenv(
    "TWITCH_CATCHUP_API",
    "https://recent-messages.robotty.de/api/v2/recent-messages",
).rstrip("/")


class TwitchCatchupMixin:
    """Expects TwitchMirrorBot attrs: connected, _queue, _msg_map, …"""

    _catchup_after_ts: Optional[float]
    _catchup_task: Optional[asyncio.Task]
    _periodic_catchup_task: Optional[asyncio.Task]
    _catchup_watermark_ts: Optional[float]

    def _init_catchup_state(self) -> None:
        self._catchup_after_ts = None
        self._catchup_task = None
        self._periodic_catchup_task = None
        self._catchup_watermark_ts = None

    async def _catchup_from_robotty(
        self,
        after_ts: float,
        *,
        announce: bool = True,
        delay: float = 0.0,
    ) -> int:
        """Fetch robotty history and enqueue unknown msgs. Returns count posted."""
        try:
            from utils.twitch_catchup import fetch_recent_messages
        except ImportError:
            from .twitch_catchup import fetch_recent_messages

        try:
            from utils.twitch_helpers import TWITCH_CHANNEL
        except ImportError:
            from .twitch_helpers import TWITCH_CHANNEL

        try:
            import aiohttp
        except ImportError:
            aiohttp = None  # type: ignore

        if aiohttp is None:
            log.warning("Catch-up skipped: aiohttp missing")
            return 0
        if delay > 0:
            await asyncio.sleep(delay)

        try:
            messages = await fetch_recent_messages(
                TWITCH_CHANNEL,
                after_ts=after_ts,
                limit=CATCHUP_LIMIT,
                api_base=CATCHUP_API,
            )
        except Exception as e:
            log.exception("Catch-up fetch failed: %s", e)
            return 0

        if not messages:
            log.debug(
                "Catch-up: nothing after %s",
                time.strftime("%H:%M:%S", time.localtime(after_ts)),
            )
            return 0

        fresh = []
        for m in messages:
            if m.twitch_id in self._msg_map or m.twitch_id in self._outbound_twitch_ids:
                continue
            if m.content.startswith("[Discord]") or m.content.lower().startswith("[discord]"):
                continue
            fresh.append(m)

        if not fresh:
            log.debug("Catch-up: %s robotty msgs, all known/skipped", len(messages))
            return 0

        log.info(
            "Catch-up: enqueue %s missed msg(s) since %s",
            len(fresh),
            time.strftime("%H:%M:%S", time.localtime(after_ts)),
        )

        if announce:
            try:
                await self.discord_bot.wait_until_ready()
                ch = self._text_channel or self.discord_bot.get_channel(
                    self.discord_channel_id
                )
                if ch is None:
                    try:
                        ch = await self.discord_bot.fetch_channel(self.discord_channel_id)
                    except Exception:
                        ch = None
                if isinstance(ch, discord.TextChannel):
                    first = fresh[0].time_label()
                    last = fresh[-1].time_label()
                    await ch.send(
                        f"♻️ **Catch-up** · `{len(fresh)}` Nachrichten nachgeholt "
                        f"(`{first}`–`{last}`, recent-messages)",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except Exception as e:
                log.warning("Catch-up notice failed: %s", e)

        for m in fresh:
            label = m.time_label()
            prefixed = f"-# ⏱ `{label}`\n{m.content}"
            await self._queue.put(
                (m.login, m.display, prefixed[:1900], m.twitch_id, None, m.is_action)
            )
            await asyncio.sleep(0.05)

        return len(fresh)

    async def _periodic_catchup_loop(self) -> None:
        if CATCHUP_INTERVAL <= 0:
            return
        log.info("Periodic catch-up every %ss (limit=%s)", CATCHUP_INTERVAL, CATCHUP_LIMIT)
        try:
            while True:
                await asyncio.sleep(float(CATCHUP_INTERVAL))
                if not getattr(self, "connected", False):
                    continue
                after = time.time() - float(CATCHUP_INTERVAL) - 30.0
                watermark = getattr(self, "_catchup_watermark_ts", None)
                if watermark is not None:
                    after = max(after, float(watermark))
                try:
                    n = await self._catchup_from_robotty(after, announce=True, delay=0.0)
                    self._catchup_watermark_ts = time.time() - 5.0
                    if n:
                        log.debug("Periodic catch-up posted %s msg(s)", n)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("Periodic catch-up error: %s", e)
        except asyncio.CancelledError:
            log.debug("Periodic catch-up stopped")

    def _start_catchup_tasks(self) -> None:
        if not CATCHUP_ENABLED:
            return
        after = getattr(self, "_catchup_after_ts", None)
        if after is not None:
            self._catchup_after_ts = None
            task = getattr(self, "_catchup_task", None)
            if task is None or task.done():
                self._catchup_task = asyncio.create_task(
                    self._catchup_from_robotty(float(after), announce=True, delay=2.0),
                    name="twitch-catchup",
                )
        if CATCHUP_INTERVAL > 0:
            pt = getattr(self, "_periodic_catchup_task", None)
            if pt is None or pt.done():
                self._periodic_catchup_task = asyncio.create_task(
                    self._periodic_catchup_loop(),
                    name="twitch-catchup-periodic",
                )

    async def _cancel_catchup_tasks(self) -> None:
        for attr in ("_catchup_task", "_periodic_catchup_task"):
            t = getattr(self, attr, None)
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


async def run_twitch_supervisor(cog: Any) -> None:
    """IRC client supervisor with catch-up window across reconnects.

    Used as TwitchMirrorCog._run_twitch body (native, no monkeypatch).
    """
    try:
        from cogs import twitch_mirror as tm
    except ImportError:
        import cogs.twitch_mirror as tm  # type: ignore

    if not hasattr(cog, "_pending_catchup_ts"):
        cog._pending_catchup_ts = None

    backoff = 5.0
    while True:
        client = None
        start_task = None
        watch_task = None
        was_ready = False
        try:
            catchup_ts = cog._pending_catchup_ts
            cog._pending_catchup_ts = None
            client = tm.TwitchMirrorBot(
                cog.bot, cog._discord_channel_id, store=cog._store
            )
            client._catchup_after_ts = catchup_ts
            cog._twitch = client
            start_task = asyncio.create_task(client.start(), name="twitch-mirror-start")
            watch_task = asyncio.create_task(
                cog._irc_watchdog(client, start_task),
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
            await cog._shutdown_client(client)
            cog._twitch = None
            break
        except Exception as e:
            log.error("Twitch run loop error: %s – retry in %.0fs", e, backoff)
        finally:
            if client is not None:
                if getattr(client, "_was_ready", False) and CATCHUP_ENABLED:
                    cog._pending_catchup_ts = float(client._last_irc_activity)
                    log.info(
                        "Catch-up window starts at %s",
                        time.strftime(
                            "%H:%M:%S",
                            time.localtime(cog._pending_catchup_ts),
                        ),
                    )
                await cog._shutdown_client(client)
            if cog._twitch is client:
                cog._twitch = None

        await asyncio.sleep(backoff)
        if was_ready:
            backoff = 5.0
        else:
            backoff = min(backoff * 1.5, 120.0)
