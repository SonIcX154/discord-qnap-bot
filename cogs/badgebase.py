"""BadgeBase watcher – Discord notify when new claimable Twitch badges appear.

Channel is configured per guild via /badgebase-channel (not .env).
Only secrets/defaults in env: BADGEBASE_API_KEY, optional BADGEBASE_TWITCH_LOGIN.
"""
from __future__ import annotations

import os
import re
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord.ext import commands
from discord import app_commands

log = logging.getLogger("qnapbot.badgebase")

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore

try:
    from utils.guild_settings import get_settings, GuildSettings
    from utils.permissions import admin_or_bot_dev, is_admin_or_bot_dev
except ImportError:
    from ..utils.guild_settings import get_settings, GuildSettings
    from ..utils.permissions import admin_or_bot_dev, is_admin_or_bot_dev

API_BASE = "https://badgebase.de/api/v1"
API_KEY = os.getenv("BADGEBASE_API_KEY", "").strip()
DEFAULT_LOGIN = os.getenv("BADGEBASE_TWITCH_LOGIN", "").strip().lstrip("@").lower()
POLL_SECONDS = max(60, int(os.getenv("BADGEBASE_POLL_SECONDS", "300")))  # default 5 min
SETTING_KEY = "badgebase.notify_channel"
SEEN_DB_PATH = os.getenv("BADGEBASE_SEEN_PATH", "data/badgebase_seen.db")

_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]+$")


class SeenStore:
    """Tracks badge ids we already notified about (global, not per-guild)."""

    def __init__(self, path: str = SEEN_DB_PATH) -> None:
        self.path = path

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with __import__("aiosqlite").connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_badges (
                    badge_id    INTEGER PRIMARY KEY,
                    title       TEXT,
                    first_seen  INTEGER NOT NULL
                )
                """
            )
            await db.commit()

    async def known_ids(self) -> set[int]:
        import aiosqlite

        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT badge_id FROM seen_badges") as cur:
                rows = await cur.fetchall()
        return {int(r[0]) for r in rows}

    async def mark(self, badge_id: int, title: str = "") -> None:
        import aiosqlite

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO seen_badges (badge_id, title, first_seen)
                VALUES (?, ?, ?)
                """,
                (int(badge_id), title or None, int(time.time())),
            )
            await db.commit()

    async def mark_many(self, badges: list[dict[str, Any]]) -> None:
        import aiosqlite

        now = int(time.time())
        rows = [
            (int(b["id"]), str(b.get("title") or ""), now)
            for b in badges
            if b.get("id") is not None
        ]
        if not rows:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                """
                INSERT OR IGNORE INTO seen_badges (badge_id, title, first_seen)
                VALUES (?, ?, ?)
                """,
                rows,
            )
            await db.commit()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _fmt_dt(value: Optional[str]) -> str:
    dt = _parse_dt(value)
    if dt is None:
        return "–"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.strftime("%d.%m.%Y %H:%M")


def _badge_embed(badge: dict[str, Any], *, prefix: str = "Neues Badge") -> discord.Embed:
    title = badge.get("title") or f"Badge #{badge.get('id')}"
    paid = bool(badge.get("paid"))
    embed = discord.Embed(
        title=f"{'💰' if paid else '🆓'} {prefix}: {title}",
        url=badge.get("url") or None,
        color=discord.Color.gold() if paid else discord.Color.green(),
        description=(
            f"**Zeitraum:** `{_fmt_dt(badge.get('startDate'))}` → "
            f"`{_fmt_dt(badge.get('endDate'))}`\n"
            f"**Typ:** {'Paid' if paid else 'Free'}\n"
            f"**Collector:** {badge.get('collectors', 0):,}"
        ),
    )
    image = badge.get("image_url")
    if image:
        embed.set_thumbnail(url=image)
    embed.set_footer(text=f"BadgeBase · id {badge.get('id')}")
    if badge.get("url"):
        embed.add_field(name="Link", value=f"[badgebase.de]({badge['url']})", inline=False)
    return embed


class BadgeBaseCog(commands.Cog):
    """Poll BadgeBase for newly claimable badges and post to configured channels."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings: GuildSettings = get_settings()
        self.seen = SeenStore()
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[Any] = None
        self._running = True

    async def cog_load(self) -> None:
        await self.settings.init()
        await self.seen.init()
        if not API_KEY:
            log.warning("BADGEBASE_API_KEY not set – badgebase cog idle")
            return
        if aiohttp is None:
            log.warning("aiohttp missing – badgebase cog idle")
            return
        self._task = asyncio.create_task(self._poll_loop())
        log.info("BadgeBase watcher started (poll every %ss)", POLL_SECONDS)

    async def cog_unload(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> Any:
        if aiohttp is None:
            raise RuntimeError("aiohttp not installed")
        if not API_KEY:
            raise RuntimeError("BADGEBASE_API_KEY not set")
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Accept": "application/json",
                    "User-Agent": "discord-qnap-bot/badgebase",
                },
            )
        return self._session

    async def _api_get(self, path: str, params: Optional[dict[str, str]] = None) -> dict[str, Any]:
        session = await self._get_session()
        url = f"{API_BASE}{path}"
        async with session.get(url, params=params or {}) as resp:
            if resp.status == 429:
                retry = int(resp.headers.get("Retry-After", "60"))
                log.warning("BadgeBase rate limited – sleep %ss", retry)
                await asyncio.sleep(retry)
                raise RuntimeError(f"Rate limited, retry after {retry}s")
            if resp.status in (401, 403):
                body = await resp.text()
                log.error("BadgeBase auth failed HTTP %s: %s", resp.status, body[:200])
                raise RuntimeError(f"Auth failed HTTP {resp.status}")
            if resp.status == 404:
                raise RuntimeError("Not found (404) – Login prüfen")
            if resp.status != 200:
                body = await resp.text()
                log.warning("BadgeBase HTTP %s: %s", resp.status, body[:200])
                raise RuntimeError(f"HTTP {resp.status}: {body[:120]}")
            data = await resp.json()
        if not isinstance(data, dict):
            return {}
        return data

    async def _fetch_claimable(self) -> list[dict[str, Any]]:
        """Full catalogue of currently claimable badges (for notify poll)."""
        data = await self._api_get("/badges", {"status": "claimable"})
        items = data.get("data")
        if not isinstance(items, list):
            return []
        return [b for b in items if isinstance(b, dict) and b.get("id") is not None]

    async def _fetch_missing(self, login: str) -> list[dict[str, Any]]:
        """Badges this user does not own yet (default: only currently claimable)."""
        login = login.strip().lstrip("@").lower()
        if not _LOGIN_RE.match(login):
            raise RuntimeError("Ungültiger Twitch-Login (nur A–Z, 0–9, _)")
        data = await self._api_get(f"/user/{login}/missing")
        items = data.get("data")
        if not isinstance(items, list):
            return []
        return [b for b in items if isinstance(b, dict) and b.get("id") is not None]

    async def _notify_guilds(self, badges: list[dict[str, Any]]) -> None:
        if not badges:
            return
        targets = await self.settings.guilds_with_key(SETTING_KEY)
        if not targets:
            log.debug("New badges found but no notify channels configured")
            return

        for guild_id, raw_cid in targets:
            try:
                channel_id = int(raw_cid)
            except ValueError:
                continue
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception as e:
                    log.warning("Cannot resolve channel %s (guild %s): %s", channel_id, guild_id, e)
                    continue
            if not isinstance(channel, discord.TextChannel):
                continue

            for badge in badges:
                try:
                    await channel.send(embed=_badge_embed(badge))
                    await asyncio.sleep(0.4)
                except discord.Forbidden:
                    log.warning("No permission to post in #%s (%s)", channel.name, channel_id)
                    break
                except Exception as e:
                    log.warning("Post failed in %s: %s", channel_id, e)

    async def _poll_once(self, *, bootstrap: bool = False) -> None:
        badges = await self._fetch_claimable()
        if not badges:
            return

        known = await self.seen.known_ids()
        if not known:
            await self.seen.mark_many(badges)
            log.info("BadgeBase bootstrap: seeded %s claimable badge(s)", len(badges))
            return

        fresh = [b for b in badges if int(b["id"]) not in known]
        if not fresh:
            return

        log.info("BadgeBase: %s new claimable badge(s)", len(fresh))
        await self._notify_guilds(fresh)
        await self.seen.mark_many(fresh)

    async def _poll_loop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(8)
        try:
            await self._poll_once(bootstrap=True)
        except Exception as e:
            log.exception("BadgeBase bootstrap failed: %s", e)

        while self._running and not self.bot.is_closed():
            try:
                await asyncio.sleep(POLL_SECONDS)
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("BadgeBase poll error: %s", e)
                await asyncio.sleep(30)

    # ------------------------------------------------------------------ commands

    @app_commands.command(
        name="badgebase-channel",
        description="Channel für neue BadgeBase-Benachrichtigungen setzen oder anzeigen",
    )
    @app_commands.describe(
        channel="Textkanal für Notifications (leer = aktuellen anzeigen / entfernen)",
        clear="Channel-Zuordnung entfernen",
    )
    @app_commands.default_permissions(manage_guild=True)
    @admin_or_bot_dev
    async def badgebase_channel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        clear: bool = False,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        if clear:
            removed = await self.settings.clear_channel(guild_id, SETTING_KEY)
            msg = (
                "✅ BadgeBase-Notifications deaktiviert."
                if removed
                else "ℹ️ Es war kein Channel gesetzt."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if channel is not None:
            await self.settings.set_channel(guild_id, SETTING_KEY, channel.id)
            await interaction.response.send_message(
                f"✅ Neue claimable Badges werden in {channel.mention} gepostet.",
                ephemeral=True,
            )
            return

        current = await self.settings.get_channel(guild_id, SETTING_KEY)
        if current:
            await interaction.response.send_message(
                f"Aktueller BadgeBase-Channel: <#{current}>\n"
                f"Ändern: `/badgebase-channel channel:#…` · Aus: `clear:True`",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Noch kein Channel gesetzt.\n"
                "Setzen: `/badgebase-channel channel:#dein-channel`",
                ephemeral=True,
            )

    @badgebase_channel.error
    async def badgebase_channel_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:  # type: ignore[misc]
        if isinstance(error, app_commands.CheckFailure):
            if interaction.response.is_done():
                return
            await interaction.response.send_message(
                "❌ Du brauchst **Manage Guild** oder musst als **Bot-Dev** hinterlegt sein.",
                ephemeral=True,
            )
        else:
            raise error

    @app_commands.command(
        name="badgebase-status",
        description="Status des BadgeBase-Watchers",
    )
    @app_commands.default_permissions(manage_guild=True)
    @admin_or_bot_dev
    async def badgebase_status(self, interaction: discord.Interaction) -> None:
        known = await self.seen.known_ids()
        channel_id = None
        if interaction.guild:
            channel_id = await self.settings.get_channel(interaction.guild.id, SETTING_KEY)

        embed = discord.Embed(
            title="BadgeBase Status",
            color=discord.Color.blurple() if API_KEY else discord.Color.orange(),
        )
        embed.add_field(
            name="API-Key",
            value="✅ gesetzt" if API_KEY else "❌ fehlt (`BADGEBASE_API_KEY`)",
            inline=True,
        )
        embed.add_field(
            name="Poll-Intervall",
            value=f"`{POLL_SECONDS}s`",
            inline=True,
        )
        embed.add_field(
            name="Bekannte Badges",
            value=f"`{len(known)}`",
            inline=True,
        )
        embed.add_field(
            name="Default-Login",
            value=f"`{DEFAULT_LOGIN}`" if DEFAULT_LOGIN else "*nicht gesetzt*",
            inline=True,
        )
        embed.add_field(
            name="Notify-Channel",
            value=f"<#{channel_id}>" if channel_id else "*nicht gesetzt*",
            inline=False,
        )
        embed.set_footer(text="Channel setzen: /badgebase-channel")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @badgebase_status.error
    async def badgebase_status_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:  # type: ignore[misc]
        if isinstance(error, app_commands.CheckFailure):
            if interaction.response.is_done():
                return
            await interaction.response.send_message(
                "❌ Du brauchst **Manage Guild** oder musst als **Bot-Dev** hinterlegt sein.",
                ephemeral=True,
            )
        else:
            raise error

    @app_commands.command(
        name="badgebase-claimable",
        description="Badges die du noch nicht hast und die gerade claimable sind",
    )
    @app_commands.describe(
        login="Twitch-Login (sonst BADGEBASE_TWITCH_LOGIN aus .env)",
        limit="Max. Einträge in der Liste (1–25, Default 15)",
        detail="Zusätzlich 1 Badge als volles Embed zeigen",
    )
    @app_commands.default_permissions(manage_guild=True)
    @admin_or_bot_dev
    async def badgebase_claimable(
        self,
        interaction: discord.Interaction,
        login: Optional[str] = None,
        limit: app_commands.Range[int, 1, 25] = 15,
        detail: bool = False,
    ) -> None:
        if not API_KEY:
            await interaction.response.send_message(
                "❌ `BADGEBASE_API_KEY` ist nicht gesetzt.",
                ephemeral=True,
            )
            return
        if aiohttp is None:
            await interaction.response.send_message(
                "❌ `aiohttp` ist nicht installiert.",
                ephemeral=True,
            )
            return

        resolved = (login or DEFAULT_LOGIN or "").strip().lstrip("@").lower()
        if not resolved:
            await interaction.response.send_message(
                "❌ Kein Twitch-Login.\n"
                "Entweder `login:` angeben oder `BADGEBASE_TWITCH_LOGIN` in der `.env` setzen.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            badges = await self._fetch_missing(resolved)
        except Exception as e:
            await interaction.followup.send(
                f"❌ API-Fehler für `{resolved}`: `{e}`",
                ephemeral=True,
            )
            return

        if not badges:
            await interaction.followup.send(
                f"✅ **@{resolved}** – keine fehlenden claimable Badges.",
                ephemeral=True,
            )
            return

        free_n = sum(1 for b in badges if not b.get("paid"))
        paid_n = len(badges) - free_n

        lines: list[str] = []
        for b in badges[: int(limit)]:
            bid = int(b["id"])
            title = b.get("title") or f"#{bid}"
            paid = "💰" if b.get("paid") else "🆓"
            link = b.get("url") or ""
            if link:
                lines.append(f"{paid} **[{title}]({link})** `{bid}`")
            else:
                lines.append(f"{paid} **{title}** `{bid}`")

        more = len(badges) - int(limit)
        footer_extra = f" · +{more} weitere" if more > 0 else ""

        embed = discord.Embed(
            title=f"Fehlende claimable Badges · @{resolved}",
            description="\n".join(lines) if lines else "–",
            color=discord.Color.green(),
        )
        embed.add_field(name="Gesamt", value=str(len(badges)), inline=True)
        embed.add_field(name="Free", value=str(free_n), inline=True)
        embed.add_field(name="Paid", value=str(paid_n), inline=True)
        embed.set_footer(text=f"GET /user/{resolved}/missing{footer_extra}")

        await interaction.followup.send(embed=embed, ephemeral=True)

        if detail and badges:
            await interaction.followup.send(
                embed=_badge_embed(badges[0], prefix="Beispiel"),
                ephemeral=True,
            )

    @badgebase_claimable.error
    async def badgebase_claimable_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:  # type: ignore[misc]
        if isinstance(error, app_commands.CheckFailure):
            if interaction.response.is_done():
                return
            await interaction.response.send_message(
                "❌ Du brauchst **Manage Guild** oder musst als **Bot-Dev** hinterlegt sein.",
                ephemeral=True,
            )
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BadgeBaseCog(bot))
