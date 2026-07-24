from __future__ import annotations

import base64
import asyncio
from typing import Any, Optional

import discord


class AlwaysTruthyDict(dict):
    """Leeres dict bleibt truthy – verhindert `overwrites or None` -> None."""

    def __bool__(self) -> bool:
        return True


def build_overwrites(
    guild: discord.Guild,
    overwrites_data: list[dict[str, Any]],
    role_map: dict[int, int],
) -> AlwaysTruthyDict:
    result: AlwaysTruthyDict = AlwaysTruthyDict()
    for ow in overwrites_data:
        target = None
        if ow.get("type") == "role":
            old_id = ow["id"]
            mapped = role_map.get(old_id)
            if old_id == guild.id or mapped == guild.default_role.id:
                target = guild.default_role
            elif mapped:
                target = guild.get_role(mapped)
        elif ow.get("type") == "member":
            member = guild.get_member(ow["id"])
            if member:
                target = member
        if target is None:
            continue
        result[target] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(ow.get("allow", 0)),
            discord.Permissions(ow.get("deny", 0)),
        )
    return result


async def apply_role_hierarchy(
    guild: discord.Guild,
    roles_data: list[dict[str, Any]],
) -> None:
    """
    Setzt Rollen-Reihenfolge wie im Snapshot (relativ),
    alle strikt unter der höchsten Bot-Rolle.
    Matching über Rollen-Namen.
    """
    if not guild.me:
        return

    bot_top = guild.me.top_role.position
    max_usable = max(1, bot_top - 1)

    # Snapshot-Rollen (nicht managed) nach Original-Position
    wanted = sorted(
        [r for r in roles_data if not r.get("managed")],
        key=lambda r: r.get("position", 0),
    )
    if not wanted:
        return

    # Name -> Role (erste nicht-managed mit dem Namen)
    by_name: dict[str, discord.Role] = {}
    for role in guild.roles:
        if role.is_default() or role.managed:
            continue
        if role.position >= bot_top:
            continue
        if role.name not in by_name:
            by_name[role.name] = role

    ordered: list[discord.Role] = []
    for rd in wanted:
        role = by_name.get(rd.get("name", ""))
        if role and role not in ordered:
            ordered.append(role)

    if not ordered:
        return

    n = len(ordered)
    positions: dict[discord.Role, int] = {}
    for i, role in enumerate(ordered):
        if n <= max_usable:
            pos = i + 1
        else:
            pos = max(1, max_usable - (n - 1 - i))
        positions[role] = pos

    try:
        await guild.edit_role_positions(
            positions=positions, reason="Backup Restore hierarchy"
        )
        print(f"[Backup] Rollen-Hierarchie gesetzt: {n} Rollen unter Position {bot_top}")
        return
    except Exception as e:
        print(f"[Backup] edit_role_positions batch fehlgeschlagen: {e}")

    for role, pos in sorted(positions.items(), key=lambda x: x[1], reverse=True):
        try:
            await role.edit(position=pos, reason="Backup Restore hierarchy")
            await asyncio.sleep(0.35)
        except Exception as e:
            print(f"[Backup] Position '{role.name}' -> {pos}: {e}")


async def fetch_icon_b64(guild: discord.Guild) -> Optional[str]:
    if not guild.icon:
        return None
    try:
        data = await guild.icon.read()
        return base64.b64encode(data).decode("ascii")
    except Exception as e:
        print(f"[Backup] Icon lesen fehlgeschlagen: {e}")
        return None


async def apply_guild_branding(guild: discord.Guild, g: dict[str, Any]) -> list[str]:
    """Stellt Server-Name und Icon wieder her."""
    applied: list[str] = []
    kwargs: dict[str, Any] = {}

    if g.get("name") and g["name"] != guild.name:
        kwargs["name"] = str(g["name"])[:100]

    icon_bytes = None
    if g.get("icon_b64"):
        try:
            icon_bytes = base64.b64decode(g["icon_b64"])
        except Exception:
            icon_bytes = None

    if icon_bytes is None and g.get("icon_url"):
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(g["icon_url"]) as resp:
                    if resp.status == 200:
                        icon_bytes = await resp.read()
        except Exception as e:
            print(f"[Backup] Icon-Download fehlgeschlagen: {e}")

    if icon_bytes:
        kwargs["icon"] = icon_bytes

    if not kwargs:
        return applied

    try:
        await guild.edit(**kwargs, reason="Backup Restore branding")
        applied.extend(kwargs.keys())
        print(f"[Backup] Guild branding: {applied}")
    except Exception as e:
        print(f"[Backup] Guild branding (batch) fehlgeschlagen: {e}")
        if "name" in kwargs:
            try:
                await guild.edit(name=kwargs["name"], reason="Backup Restore name")
                applied.append("name")
            except Exception as e2:
                print(f"[Backup] Guild name fehlgeschlagen: {e2}")
        if "icon" in kwargs:
            try:
                await guild.edit(icon=kwargs["icon"], reason="Backup Restore icon")
                applied.append("icon")
            except Exception as e2:
                print(f"[Backup] Guild icon fehlgeschlagen: {e2}")
    return applied
