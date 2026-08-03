"""Shared permission helpers for admin / bot-dev checks."""
from __future__ import annotations

import os
import discord
from discord import app_commands


def parse_bot_dev_ids() -> set[int]:
    """Parse BOT_DEV_ID / BOT_DEV_IDS (comma or semicolon separated)."""
    raw = (os.getenv("BOT_DEV_ID") or os.getenv("BOT_DEV_IDS") or "").strip()
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


BOT_DEV_IDS: set[int] = parse_bot_dev_ids()


def is_bot_dev(user_id: int) -> bool:
    return user_id in BOT_DEV_IDS


def is_admin_or_bot_dev(interaction: discord.Interaction) -> bool:
    """BOT_DEV_ID(s) OR Manage Guild on the current server."""
    if interaction.user.id in BOT_DEV_IDS:
        return True
    if (
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    ):
        return True
    return False


# Ready-made check for @app_commands.check(...)
admin_or_bot_dev = app_commands.check(is_admin_or_bot_dev)
