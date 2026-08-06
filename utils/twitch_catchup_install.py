"""Deprecated – catch-up is native on TwitchMirrorBot via TwitchCatchupMixin.

Kept so older imports of ``wire_catchup`` / ``install_catchup`` do not crash.
"""
from __future__ import annotations

import logging

log = logging.getLogger("qnapbot.twitch_catchup")


def wire_catchup() -> None:
    log.debug("wire_catchup() is a no-op (catch-up is native in twitch_mirror)")


install_catchup = wire_catchup
