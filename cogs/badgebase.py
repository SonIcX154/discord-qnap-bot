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
from typing import Any, Literal, Optional

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

PriceFilter = Literal["all", "free", "paid"]


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


def _is_paid(badge: dict[str, Any]) -> bool:
    raw = badge.get("paid")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "paid")
    return False


def _badge_embed(badge: dict[str, Any], *, prefix: str = "Neues Badge") -> discord.Embed:
    title = badge.get("title") or f"Badge #{badge.get('id')}"
    paid = _is_paid(badge)
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

    async def _fetch_claimable(self, *, price: Optional[str] = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {"status": "claimable"}
        if price in ("free", "paid"):
            params["price"] = price
        data = await self._api_get("/badges", params)
        items = data.get("data")
        if not isinstance(items, list):
            return []
        return [b for b in items if isinstance(b, dict) and b.get("id") is not None]

    async def _paid_id_set(self) -> set[int]:
        paid = await self._fetch_claimable(price="paid")
        return {int(b["id"]) for b in paid}

    async def _enrich_paid_flags(self, badges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not badges:
            return badges
        try:
            paid_ids = await self._paid_id_set()
        except Exception as e:
            log.warning("Could not load paid catalogue for enrichment: %s", e)
            paid_ids = set()

        out: list[dict[str, Any]] = []
        for b in badges:
            copy = dict(b)
            bid = int(copy["id"])
            if bid in paid_ids:
                copy["paid"] = True
            elif "paid" not in copy or copy.get("paid") is None:
                copy["paid"] = False
            else:
                copy["paid"] = _is_paid(copy)
            out.append(copy)
        return out

    async def _fetch_missing(self, login: str) -> list[dict[str, Any]]:
        login = login.strip().lstrip("@").lower()
        if not _LOGIN_RE.match(login):
            raise RuntimeError("Ungültiger Twitch-Login (nur A–Z, 0–9, _)")
        data = await self._api_get(f"/user/{login}/missing")
        items = data.get("data")
        if not isinstance(items, list):
            return []
        raw = [b for b in items if isinstance(b, dict) and b.get("id") is not None]
        return await self._enrich_paid_flags(raw)

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

        try:
            fresh = await self._enrich_paid_flags(fresh)
        except Exception:
            pass

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
        name="badgebase-claimable",
        description="Badges die du noch nicht hast und die gerade claimable sind",
    )
    @app_commands.describe(
        login="Twitch-Login (sonst BADGEBASE_TWITCH_LOGIN aus .env)",
        price="Nur free, nur paid, oder alle",
        limit="Max. Einträge in der Liste (1–25, Default 15)",
    )
    @app_commands.choices(
        price=[
            app_commands.Choice(name="Alle", value="all"),
            app_commands.Choice(name="Nur Free", value="free"),
            app_commands.Choice(name="Nur Paid", value="paid"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @admin_or_bot_dev
    async def badgebase_claimable(
        self,
        interaction: discord.Interaction,
        login: Optional[str] = None,
        price: app_commands.Choice[str] = None,  # type: ignore[assignment]
        limit: app_commands.Range[int, 1, 25] = 15,
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

        price_filter: PriceFilter = "all"
        if price is not None:
            price_filter = price.value  # type: ignore[assignment]

        await interaction.response.defer(ephemeral=True)
        try:
            badges = await self._fetch_missing(resolved)
        except Exception as e:
            await interaction.followup.send(
                f"❌ API-Fehler für `{resolved}`: `{e}`",
                ephemeral=True,
            )
            return

        if price_filter == "free":
            badges = [b for b in badges if not _is_paid(b)]
        elif price_filter == "paid":
            badges = [b for b in badges if _is_paid(b)]

        if not badges:
            label = {
                "all": "keine fehlenden claimable Badges",
                "free": "keine fehlenden **free** claimable Badges",
                "paid": "keine fehlenden **paid** claimable Badges",
            }[price_filter]
            await interaction.followup.send(
                f"✅ **@{resolved}** – {label}.",
                ephemeral=True,
            )
            return

        free_n = sum(1 for b in badges if not _is_paid(b))
        paid_n = sum(1 for b in badges if _is_paid(b))

        lines: list[str] = []
        for b in badges[: int(limit)]:
            bid = int(b["id"])
            title = b.get("title") or f"#{bid}"
            icon = "💰" if _is_paid(b) else "🆓"
            link = b.get("url") or ""
            if link:
                lines.append(f"{icon} **[{title}]({link})** `{bid}`")
            else:
                lines.append(f"{icon} **{title}** `{bid}`")

        more = len(badges) - int(limit)
        footer_extra = f" · +{more} weitere" if more > 0 else ""
        filter_label = {"all": "alle", "free": "nur free", "paid": "nur paid"}[price_filter]

        embed = discord.Embed(
            title=f"Fehlende claimable Badges · @{resolved}",
            description="\n".join(lines) if lines else "–",
            color=discord.Color.gold() if price_filter == "paid" else discord.Color.green(),
        )
        embed.add_field(name="Gesamt", value=str(len(badges)), inline=True)
        embed.add_field(name="Free", value=str(free_n), inline=True)
        embed.add_field(name="Paid", value=str(paid_n), inline=True)
        embed.set_footer(text=f"/user/{resolved}/missing · Filter: {filter_label}{footer_extra}")

        await interaction.followup.send(embed=embed, ephemeral=True)

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
