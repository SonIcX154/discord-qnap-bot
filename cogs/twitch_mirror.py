from __future__ import annotations

# Full module lives in utils.twitch_mirror_core (avoids oversized single-file push).
from utils.twitch_mirror_core import *  # noqa: F401,F403
from utils.twitch_mirror_core import TwitchMirrorBot, TwitchMirrorCog  # noqa: F401


async def setup(bot):
    from discord.ext import commands

    await bot.add_cog(TwitchMirrorCog(bot))
