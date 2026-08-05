"""Runtime patches: catch-up missed Twitch chat via robotty after IRC reconnect."""
from __future__ import annotations

import os
import time
import asyncio
import logging
from typing import Optional, Any

import discord

log = logging.getLogger("qnapbot.twitch_catchup")

CATCHUP_ENABLED = os.getenv("TWITCH_CATCHUP", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
CATCHUP_LIMIT = max(10, min(500, int(os.getenv("TWITCH_CATCHUP_LIMIT", "100"))))
CATCHUP_API = os.getenv(
    "TWITCH_CATCHUP_API",
    "https://recent-messages.robotty.de/api/v2/recent-messages",
).rstrip("/")

_installed = False


def install_catchup() -> None:
    """Monkey-patch TwitchMirrorBot / TwitchMirrorCog for robotty catch-up."""
    global _installed
    if _installed:
        return

    try:
        from cogs import twitch_mirror as tm
    except ImportError:
        from . import twitch_mirror as tm  # type: ignore

    try:
        from utils.twitch_catchup import fetch_recent_messages
    except ImportError:
        from .twitch_catchup import fetch_recent_messages

    TwitchMirrorBot = tm.TwitchMirrorBot
    TwitchMirrorCog = tm.TwitchMirrorCog

    # --- Bot: catch-up runner -------------------------------------------------
    async def _catchup_from_robotty(self: Any, after_ts: float) -> None:
        aiohttp = tm.aiohttp
        if aiohttp is None:
            log.warning("Catch-up skipped: aiohttp missing")
            return
        await asyncio.sleep(2.0)
        try:
            messages = await fetch_recent_messages(
                tm.TWITCH_CHANNEL,
                after_ts=after_ts,
                limit=CATCHUP_LIMIT,
                api_base=CATCHUP_API,
            )
        except Exception as e:
            log.exception("Catch-up fetch failed: %s", e)
            return

        if not messages:
            log.info(
                "Catch-up: no messages after %s (robotty)",
                time.strftime("%H:%M:%S", time.localtime(after_ts)),
            )
            return

        fresh = []
        for m in messages:
            if m.twitch_id in self._msg_map or m.twitch_id in self._outbound_twitch_ids:
                continue
            if m.content.startswith("[Discord]") or m.content.lower().startswith("[discord]"):
                continue
            fresh.append(m)

        if not fresh:
            log.info(
                "Catch-up: %s robotty msgs, all already known/skipped",
                len(messages),
            )
            return

        log.info(
            "Catch-up: enqueue %s missed msg(s) since %s",
            len(fresh),
            time.strftime("%H:%M:%S", time.localtime(after_ts)),
        )

        try:
            await self.discord_bot.wait_until_ready()
            ch = self._text_channel or self.discord_bot.get_channel(self.discord_channel_id)
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

    TwitchMirrorBot._catchup_from_robotty = _catchup_from_robotty  # type: ignore[attr-defined]

    orig_ready = TwitchMirrorBot.event_ready

    async def event_ready(self: Any) -> None:
        await orig_ready(self)
        after = getattr(self, "_catchup_after_ts", None)
        if not CATCHUP_ENABLED or after is None:
            return
        self._catchup_after_ts = None
        task = getattr(self, "_catchup_task", None)
        if task is not None and not task.done():
            return
        self._catchup_task = asyncio.create_task(
            self._catchup_from_robotty(float(after)),
            name="twitch-catchup",
        )

    TwitchMirrorBot.event_ready = event_ready  # type: ignore[method-assign]

    # --- Cog: remember window + pass to new client -----------------------------
    orig_run = TwitchMirrorCog._run_twitch

    async def _run_twitch(self: Any) -> None:
        """Wrap original supervisor to record catch-up window and pass it on."""
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
                client._catchup_after_ts = catchup_ts  # type: ignore[attr-defined]
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

    # Ensure pending attr exists on new cogs
    orig_cog_init = TwitchMirrorCog.__init__

    def cog_init(self: Any, bot: Any) -> None:
        orig_cog_init(self, bot)
        self._pending_catchup_ts = None

    TwitchMirrorCog.__init__ = cog_init  # type: ignore[method-assign]

    _installed = True
    log.info(
        "Catch-up install: enabled=%s limit=%s api=%s",
        CATCHUP_ENABLED,
        CATCHUP_LIMIT,
        CATCHUP_API,
    )
