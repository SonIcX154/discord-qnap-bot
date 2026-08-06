from __future__ import annotations

"""Twitch mirror cog.

IMPORTANT: This is a temporary stub. The full file with native
TwitchCatchupMixin integration is ready locally but could not be
pushed in one piece due to tool size limits after a bad truncate.

Replace this file with the full implementation from the conversation
artifact / local clone before deploying.
"""

import logging
from discord.ext import commands

log = logging.getLogger("qnapbot.twitch_mirror")


class TwitchMirrorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        log.error(
            "Twitch mirror is a STUB – deploy the full cogs/twitch_mirror.py "
            "(native catch-up version) before relying on the mirror."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TwitchMirrorCog(bot))
