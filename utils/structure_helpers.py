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


def _can_manage_role(me: discord.Member, role: discord.Role) -> tuple[bool, str]:
    """
    Discord hierarchy rules (independent of Administrator):
    - cannot touch @everyone
    - cannot touch managed roles (bots, integrations, boosts)
    - can only manage roles *strictly below* the bot's highest role
    """
    if role.is_default():
        return False, "@everyone"
    if role.managed:
        return False, "managed (bot/integration/boost)"
    if role >= me.top_role:
        return False, (
            f"hierarchy (role pos={role.position} >= bot top "
            f"'{me.top_role.name}' pos={me.top_role.position})"
        )
    if not (me.guild_permissions.manage_roles or me.guild_permissions.administrator):
        return False, "missing Manage Roles / Administrator permission"
    return True, ""


async def clear_manageable_roles(guild: discord.Guild) -> int:
    me = guild.me
    if me is None:
        print("[Backup] clear roles: guild.me fehlt")
        return 0

    if not (me.guild_permissions.manage_roles or me.guild_permissions.administrator):
        print(
            "[Backup] clear roles: Bot hat weder Manage Roles noch Administrator – Abbruch"
        )
        return 0

    bot_top = me.top_role
    print(
        f"[Backup] clear roles: bot='{me.display_name}' top_role='{bot_top.name}' "
        f"pos={bot_top.position} admin={me.guild_permissions.administrator} "
        f"manage_roles={me.guild_permissions.manage_roles}"
    )

    # Snapshot sorted high → low (delete higher first so hierarchy stays consistent)
    roles = sorted(list(guild.roles), key=lambda r: r.position, reverse=True)
    print(f"[Backup] clear roles: {len(roles)} Rollen im Cache")

    deleted = 0
    skipped: list[str] = []
    failed: list[str] = []

    for role in roles:
        ok, reason = _can_manage_role(me, role)
        if not ok:
            skipped.append(f"{role.name} ({reason})")
            continue

        try:
            await _with_timeout(
                role.delete(reason="Backup Restore: clear roles"),
                15.0,
                f"role.delete {role.name}",
            )
            deleted += 1
            print(f"[Backup] Rolle gelöscht: {role.name} (pos={role.position})")
            await asyncio.sleep(0.4)
        except discord.Forbidden as e:
            failed.append(f"{role.name}: Forbidden ({e})")
            print(f"[Backup] Rolle löschen Forbidden '{role.name}': {e}")
        except discord.HTTPException as e:
            failed.append(f"{role.name}: HTTP {e.status} ({e})")
            print(f"[Backup] Rolle löschen HTTP '{role.name}': {e}")
        except Exception as e:
            failed.append(f"{role.name}: {e}")
            print(f"[Backup] Rolle löschen '{role.name}': {e}")

    # Second pass: Discord sometimes keeps stale hierarchy after bulk deletes.
    # Refresh member/role cache and try remaining roles once more.
    if deleted > 0 or failed:
        await asyncio.sleep(1.0)
        try:
            # Force a lightweight refresh of the guild's role list via HTTP
            fetched = await guild.fetch_roles()
            remaining = sorted(fetched, key=lambda r: r.position, reverse=True)
        except Exception as e:
            print(f"[Backup] clear roles refresh fehlgeschlagen: {e}")
            remaining = sorted(list(guild.roles), key=lambda r: r.position, reverse=True)

        # Re-resolve me after potential role changes
        me = guild.me or me
        for role in remaining:
            ok, reason = _can_manage_role(me, role)
            if not ok:
                continue
            try:
                await _with_timeout(
                    role.delete(reason="Backup Restore: clear roles (pass 2)"),
                    15.0,
                    f"role.delete.pass2 {role.name}",
                )
                deleted += 1
                print(f"[Backup] Rolle gelöscht (pass 2): {role.name}")
                await asyncio.sleep(0.4)
            except Exception as e:
                print(f"[Backup] Rolle löschen pass2 '{role.name}': {e}")
                failed.append(f"{role.name} (pass2): {e}")

    print(f"[Backup] clear roles fertig: {deleted} gelöscht")
    if skipped:
        # Only log a summary – can be long
        print(f"[Backup] clear roles übersprungen ({len(skipped)}): " + "; ".join(skipped[:15]))
        if len(skipped) > 15:
            print(f"[Backup]   … und {len(skipped) - 15} weitere")
    if failed:
        print(f"[Backup] clear roles fehlgeschlagen ({len(failed)}): " + "; ".join(failed[:10]))

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
    me = guild.me
    if me is None:
        return 0

    by_name: dict[str, list[discord.Role]] = {}
    for role in guild.roles:
        if role.is_default() or role.managed:
            continue
        by_name.setdefault(role.name, []).append(role)

    removed = 0
    for name, roles in by_name.items():
        if len(roles) < 2:
            continue
        # Keep the highest; delete the rest if manageable
        roles_sorted = sorted(roles, key=lambda r: r.position, reverse=True)
        for role in roles_sorted[1:]:
            ok, _ = _can_manage_role(me, role)
            if not ok:
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


async def convert_to_news_channels(
    guild: discord.Guild,
    channels_data: list[dict[str, Any]],
) -> int:
    """
    Wandelt Text-Channels, die im Snapshot Typ 5 (Announcement) hatten,
    in News-Channels um. Braucht Community-Features auf dem Server.
    """
    wanted_names = {
        str(ch.get("name"))
        for ch in channels_data
        if ch.get("type") == 5 and ch.get("name")
    }
    if not wanted_names:
        return 0

    converted = 0
    for channel in list(guild.text_channels):
        if channel.name not in wanted_names:
            continue
        if getattr(channel, "is_news", lambda: False)():
            print(f"[Backup] #{channel.name} ist bereits News-Channel")
            continue
        try:
            await _with_timeout(
                channel.edit(
                    type=discord.ChannelType.news,
                    reason="Backup Restore: Announcement Channel",
                ),
                15.0,
                f"channel.edit news {channel.name}",
            )
            converted += 1
            print(f"[Backup] #{channel.name} → Announcement/News")
            await asyncio.sleep(0.4)
        except Exception as e:
            print(
                f"[Backup] #{channel.name} konnte nicht zu News werden: {e} "
                f"(Server braucht ggf. Community-Features)"
            )
    return converted


async def apply_role_hierarchy(
    guild: discord.Guild,
    roles_data: list[dict[str, Any]],
) -> None:
    me = guild.me
    if me is None:
        return

    await dedupe_roles_by_name(guild)

    bot_top = me.top_role.position
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
        if role >= me.top_role:
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
