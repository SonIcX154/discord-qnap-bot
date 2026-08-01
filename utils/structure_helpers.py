from __future__ import annotations

import base64
import asyncio
import logging
from typing import Any, Optional

import discord

log = logging.getLogger("qnapbot.backup.structure")


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
        log.warning("TIMEOUT (%ss) bei: %s", seconds, label)
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
        log.warning("clear roles: guild.me fehlt")
        return 0

    if not (me.guild_permissions.manage_roles or me.guild_permissions.administrator):
        log.warning(
            "clear roles: Bot hat weder Manage Roles noch Administrator – Abbruch"
        )
        return 0

    bot_top = me.top_role
    log.info(
        "clear roles: bot='%s' top_role='%s' pos=%s admin=%s manage_roles=%s",
        me.display_name, bot_top.name, bot_top.position,
        me.guild_permissions.administrator, me.guild_permissions.manage_roles,
    )

    # Snapshot sorted high → low (delete higher first so hierarchy stays consistent)
    roles = sorted(list(guild.roles), key=lambda r: r.position, reverse=True)
    log.info("clear roles: %s Rollen im Cache", len(roles))

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
            log.debug("Rolle gelöscht: %s (pos=%s)", role.name, role.position)
            await asyncio.sleep(0.4)
        except discord.Forbidden as e:
            failed.append(f"{role.name}: Forbidden ({e})")
            log.warning("Rolle löschen Forbidden '%s': %s", role.name, e)
        except discord.HTTPException as e:
            failed.append(f"{role.name}: HTTP {e.status} ({e})")
            log.warning("Rolle löschen HTTP '%s': %s", role.name, e)
        except Exception as e:
            failed.append(f"{role.name}: {e}")
            log.warning("Rolle löschen '%s': %s", role.name, e)

    # Second pass: Discord sometimes keeps stale hierarchy after bulk deletes.
    # Refresh member/role cache and try remaining roles once more.
    if deleted > 0 or failed:
        await asyncio.sleep(1.0)
        try:
            # Force a lightweight refresh of the guild's role list via HTTP
            fetched = await guild.fetch_roles()
            remaining = sorted(fetched, key=lambda r: r.position, reverse=True)
        except Exception as e:
            log.warning("clear roles refresh fehlgeschlagen: %s", e)
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
                log.debug("Rolle gelöscht (pass 2): %s", role.name)
                await asyncio.sleep(0.4)
            except Exception as e:
                log.warning("Rolle löschen pass2 '%s': %s", role.name, e)
                failed.append(f"{role.name} (pass2): {e}")

    log.info("clear roles fertig: %s gelöscht", deleted)
    if skipped:
        log.debug(
            "clear roles übersprungen (%s): %s",
            len(skipped), "; ".join(skipped[:15]),
        )
        if len(skipped) > 15:
            log.debug("  … und %s weitere", len(skipped) - 15)
    if failed:
        log.warning(
            "clear roles fehlgeschlagen (%s): %s",
            len(failed), "; ".join(failed[:10]),
        )

    return deleted


async def clear_channels(guild: discord.Guild, keep_channel_id: Optional[int] = None) -> int:
    deleted = 0
    channels = list(guild.channels)
    log.info("clear channels: %s (keep=%s)", len(channels), keep_channel_id)
    for channel in channels:
        if keep_channel_id and channel.id == keep_channel_id:
            log.debug("Channel behalten: #%s", getattr(channel, "name", channel.id))
            continue
        try:
            await _with_timeout(
                channel.delete(reason="Backup Restore: clear_first"),
                15.0,
                f"channel.delete {getattr(channel, 'name', channel.id)}",
            )
            deleted += 1
            log.debug("Channel gelöscht: %s", getattr(channel, "name", channel.id))
            await asyncio.sleep(0.35)
        except Exception as e:
            log.warning(
                "Channel-Löschen fehlgeschlagen (%s): %s",
                getattr(channel, "name", channel.id), e,
            )
    log.info("clear channels fertig: %s gelöscht", deleted)
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
                log.warning("Dedupe '%s': %s", name, e)
    if removed:
        log.info("%s doppelte Rollen entfernt", removed)
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
            log.debug("#%s ist bereits News-Channel", channel.name)
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
            log.info("#%s → Announcement/News", channel.name)
            await asyncio.sleep(0.4)
        except Exception as e:
            log.warning(
                "#%s konnte nicht zu News werden: %s "
                "(Server braucht ggf. Community-Features)",
                channel.name, e,
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
        log.info("Hierarchie: keine matchenden Rollen")
        return

    positions: dict[discord.Role, int] = {}
    for i, role in enumerate(ordered):
        pos = max_usable - i
        if pos < 1:
            pos = 1
        positions[role] = pos

    preview = ", ".join(f"{r.name}→{positions[r]}" for r in ordered[:5])
    log.info("Hierarchie Mapping (Top→…): Bot@%s: %s", bot_top, preview)

    try:
        await _with_timeout(
            guild.edit_role_positions(
                positions=positions, reason="Backup Restore hierarchy"
            ),
            30.0,
            "edit_role_positions",
        )
        log.info("Hierarchie OK: %s Rollen", len(ordered))
        return
    except Exception as e:
        log.warning("edit_role_positions batch: %s", e)

    for role, pos in sorted(positions.items(), key=lambda x: x[1], reverse=True):
        try:
            await _with_timeout(
                role.edit(position=pos, reason="Backup Restore hierarchy"),
                15.0,
                f"role.edit pos {role.name}",
            )
            await asyncio.sleep(0.35)
        except Exception as e:
            log.warning("Position '%s' -> %s: %s", role.name, pos, e)


async def fetch_icon_b64(guild: discord.Guild) -> Optional[str]:
    if not guild.icon:
        return None
    try:
        data = await guild.icon.read()
        return base64.b64encode(data).decode("ascii")
    except Exception as e:
        log.warning("Icon lesen fehlgeschlagen: %s", e)
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
            log.warning("Icon-Download fehlgeschlagen: %s", e)

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
        log.info("Guild branding: %s", applied)
    except Exception as e:
        log.warning("Guild branding: %s", e)
        if "name" in kwargs:
            try:
                await guild.edit(name=kwargs["name"], reason="Backup Restore name")
                applied.append("name")
            except Exception as e2:
                log.warning("Guild name: %s", e2)
        if "icon" in kwargs:
            try:
                await guild.edit(icon=kwargs["icon"], reason="Backup Restore icon")
                applied.append("icon")
            except Exception as e2:
                log.warning("Guild icon: %s", e2)
    return applied
