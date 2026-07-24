from __future__ import annotations

import base64
import asyncio
from typing import Any, Optional

import discord


class AlwaysTruthyDict(dict):
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


async def _with_timeout(coro, seconds: float, label: str):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        print(f"[Backup] TIMEOUT ({seconds}s) bei: {label}")
        raise


async def clear_manageable_roles(guild: discord.Guild) -> int:
    if not guild.me:
        print("[Backup] clear roles: guild.me fehlt")
        return 0
    bot_top = guild.me.top_role.position
    deleted = 0
    roles = sorted(list(guild.roles), key=lambda r: r.position, reverse=True)
    print(f"[Backup] clear roles: {len(roles)} Rollen, bot_top={bot_top}")
    for role in roles:
        if role.is_default() or role.managed:
            continue
        if role.position >= bot_top:
            print(f"[Backup] Rolle '{role.name}' zu hoch – skip")
            continue
        try:
            await _with_timeout(
                role.delete(reason="Backup Restore: clear roles"),
                15.0,
                f"role.delete {role.name}",
            )
            deleted += 1
            print(f"[Backup] Rolle gelöscht: {role.name}")
            await asyncio.sleep(0.35)
        except Exception as e:
            print(f"[Backup] Rolle löschen '{role.name}': {e}")
    print(f"[Backup] clear roles fertig: {deleted} gelöscht")
    return deleted


async def clear_channels(guild: discord.Guild, keep_channel_id: Optional[int] = None) -> int:
    deleted = 0
    channels = list(guild.channels)
    print(f"[Backup] clear channels: {len(channels)} (keep={keep_channel_id})")
    for channel in channels:
        if keep_channel_id and channel.id == keep_channel_id:
            print(f"[Backup] Channel behalten: #{getattr(channel, 'name', channel.id)}")
            continue
        try:
            await _with_timeout(
                channel.delete(reason="Backup Restore: clear_first"),
                15.0,
                f"channel.delete {getattr(channel, 'name', channel.id)}",
            )
            deleted += 1
            print(f"[Backup] Channel gelöscht: {getattr(channel, 'name', channel.id)}")
            await asyncio.sleep(0.35)
        except Exception as e:
            print(f"[Backup] Channel-Löschen fehlgeschlagen ({getattr(channel, 'name', channel.id)}): {e}")
    print(f"[Backup] clear channels fertig: {deleted} gelöscht")
    return deleted


async def dedupe_roles_by_name(guild: discord.Guild) -> int:
    if not guild.me:
        return 0
    bot_top = guild.me.top_role.position
    by_name: dict[str, list[discord.Role]] = {}
    for role in guild.roles:
        if role.is_default() or role.managed:
            continue
        by_name.setdefault(role.name, []).append(role)

    removed = 0
    for name, roles in by_name.items():
        if len(roles) < 2:
            continue
        roles_sorted = sorted(roles, key=lambda r: r.position, reverse=True)
        for role in roles_sorted[1:]:
            if role.position >= bot_top:
                continue
            try:
                await _with_timeout(
                    role.delete(reason="Backup Restore: dedupe"),
                    15.0,
                    f"dedupe {name}",
                )
                removed += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"[Backup] Dedupe '{name}': {e}")
    if removed:
        print(f"[Backup] {removed} doppelte Rollen entfernt")
    return removed


async def apply_role_hierarchy(
    guild: discord.Guild,
    roles_data: list[dict[str, Any]],
) -> None:
    if not guild.me:
        return

    await dedupe_roles_by_name(guild)

    bot_top = guild.me.top_role.position
    max_usable = max(1, bot_top - 1)

    wanted = sorted(
        [r for r in roles_data if not r.get("managed") and r.get("name")],
        key=lambda r: r.get("position", 0),
        reverse=True,
    )
    if not wanted:
        return

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
        role = by_name.get(rd["name"])
        if role and role not in ordered:
            ordered.append(role)

    if not ordered:
        print("[Backup] Hierarchie: keine matchenden Rollen")
        return

    positions: dict[discord.Role, int] = {}
    for i, role in enumerate(ordered):
        pos = max_usable - i
        if pos < 1:
            pos = 1
        positions[role] = pos

    preview = ", ".join(f"{r.name}→{positions[r]}" for r in ordered[:5])
    print(f"[Backup] Hierarchie Mapping (Top→…): Bot@{bot_top}: {preview}")

    try:
        await _with_timeout(
            guild.edit_role_positions(
                positions=positions, reason="Backup Restore hierarchy"
            ),
            30.0,
            "edit_role_positions",
        )
        print(f"[Backup] Hierarchie OK: {len(ordered)} Rollen")
        return
    except Exception as e:
        print(f"[Backup] edit_role_positions batch: {e}")

    for role, pos in sorted(positions.items(), key=lambda x: x[1], reverse=True):
        try:
            await _with_timeout(
                role.edit(position=pos, reason="Backup Restore hierarchy"),
                15.0,
                f"role.edit pos {role.name}",
            )
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

            timeout = aiohttp.ClientTimeout(total=20)
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
        await _with_timeout(
            guild.edit(**kwargs, reason="Backup Restore branding"),
            30.0,
            "guild.edit branding",
        )
        applied.extend(kwargs.keys())
        print(f"[Backup] Guild branding: {applied}")
    except Exception as e:
        print(f"[Backup] Guild branding: {e}")
        if "name" in kwargs:
            try:
                await guild.edit(name=kwargs["name"], reason="Backup Restore name")
                applied.append("name")
            except Exception as e2:
                print(f"[Backup] Guild name: {e2}")
        if "icon" in kwargs:
            try:
                await guild.edit(icon=kwargs["icon"], reason="Backup Restore icon")
                applied.append("icon")
            except Exception as e2:
                print(f"[Backup] Guild icon: {e2}")
    return applied
