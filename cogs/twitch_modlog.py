"""Twitch moderation events → Discord via EventSub WebSocket."""
from __future__ import annotations

import os
import json
import asyncio
from typing import Any, Optional

import discord
from discord.ext import commands
from discord import app_commands

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore

try:
    from utils.twitch_helpers import (
        TWITCH_TOKEN,
        TWITCH_CHANNEL,
        TWITCH_CLIENT_ID,
        bearer_token,
        normalize_token,
    )
except ImportError:
    from ..utils.twitch_helpers import (
        TWITCH_TOKEN,
        TWITCH_CHANNEL,
        TWITCH_CLIENT_ID,
        bearer_token,
        normalize_token,
    )


MOD_LOG_CHANNEL_ID = os.getenv("TWITCH_MOD_LOG_CHANNEL_ID", "").strip()
EVENTSUB_WS = "wss://eventsub.wss.twitch.tv/ws"

# (type, version, needs_moderator_id)
SUBSCRIPTIONS: list[tuple[str, str, bool]] = [
    ("channel.ban", "1", False),
    ("channel.unban", "1", False),
    ("channel.moderator.add", "1", False),
    ("channel.moderator.remove", "1", False),
    ("channel.shoutout.create", "1", True),
    ("channel.raid", "1", False),  # to_broadcaster condition
]


def _enabled() -> bool:
    return bool(
        MOD_LOG_CHANNEL_ID
        and TWITCH_TOKEN
        and TWITCH_CLIENT_ID
        and TWITCH_CHANNEL
        and aiohttp is not None
    )


class TwitchModLogCog(commands.Cog):
    """Posts Twitch mod / channel events into a Discord text channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[Any] = None
        self._broadcaster_id: Optional[str] = None
        self._user_id: Optional[str] = None  # token user (mod / bot)
        self._client_id: str = TWITCH_CLIENT_ID
        self._discord_channel_id: int = 0
        try:
            if MOD_LOG_CHANNEL_ID:
                self._discord_channel_id = int(MOD_LOG_CHANNEL_ID)
        except ValueError:
            self._discord_channel_id = 0

    async def cog_load(self) -> None:
        if not _enabled():
            if MOD_LOG_CHANNEL_ID and aiohttp is None:
                print("[TwitchModLog] Disabled – aiohttp missing")
            elif MOD_LOG_CHANNEL_ID:
                print(
                    "[TwitchModLog] Disabled – need TWITCH_TOKEN, TWITCH_CLIENT_ID, "
                    "TWITCH_CHANNEL, TWITCH_MOD_LOG_CHANNEL_ID"
                )
            else:
                print("[TwitchModLog] Idle (set TWITCH_MOD_LOG_CHANNEL_ID to enable)")
            return
        if not self._discord_channel_id:
            print(f"[TwitchModLog] Invalid TWITCH_MOD_LOG_CHANNEL_ID: {MOD_LOG_CHANNEL_ID!r}")
            return
        self._task = asyncio.create_task(self._run_loop())
        print(f"[TwitchModLog] Starting EventSub → channel {self._discord_channel_id}")

    async def cog_unload(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer_token(TWITCH_TOKEN)}",
            "Client-Id": self._client_id,
            "Content-Type": "application/json",
        }

    async def _resolve_ids(self, session: Any) -> bool:
        bearer = bearer_token(TWITCH_TOKEN)
        async with session.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {bearer}"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"[TwitchModLog] Token validate HTTP {resp.status}: {body[:200]}")
                return False
            info = await resp.json()

        self._user_id = str(info.get("user_id") or "") or None
        token_cid = str(info.get("client_id") or "")
        if token_cid:
            self._client_id = token_cid
        scopes = list(info.get("scopes") or [])
        print(
            f"[TwitchModLog] Token user_id={self._user_id} "
            f"scopes={len(scopes)} client={self._client_id[:8]}…"
        )

        async with session.get(
            f"https://api.twitch.tv/helix/users?login={TWITCH_CHANNEL}",
            headers=self._headers(),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"[TwitchModLog] Helix users HTTP {resp.status}: {body[:200]}")
                return False
            data = await resp.json()
        users = data.get("data") or []
        if not users:
            print(f"[TwitchModLog] Channel not found: {TWITCH_CHANNEL}")
            return False
        self._broadcaster_id = str(users[0]["id"])
        print(f"[TwitchModLog] Broadcaster {TWITCH_CHANNEL} id={self._broadcaster_id}")
        return True

    async def _subscribe_all(self, session: Any, session_id: str) -> None:
        assert self._broadcaster_id
        for event_type, version, needs_mod in SUBSCRIPTIONS:
            condition: dict[str, str] = {}
            if event_type == "channel.raid":
                condition["to_broadcaster_user_id"] = self._broadcaster_id
            else:
                condition["broadcaster_user_id"] = self._broadcaster_id
                if needs_mod:
                    if not self._user_id:
                        print(f"[TwitchModLog] Skip {event_type} (no moderator id)")
                        continue
                    condition["moderator_user_id"] = self._user_id

            payload = {
                "type": event_type,
                "version": version,
                "condition": condition,
                "transport": {"method": "websocket", "session_id": session_id},
            }
            try:
                async with session.post(
                    "https://api.twitch.tv/helix/eventsub/subscriptions",
                    headers=self._headers(),
                    json=payload,
                ) as resp:
                    body = await resp.text()
                    if resp.status in (200, 202):
                        print(f"[TwitchModLog] Subscribed: {event_type}")
                    else:
                        print(
                            f"[TwitchModLog] Subscribe {event_type} "
                            f"HTTP {resp.status}: {body[:250]}"
                        )
            except Exception as e:
                print(f"[TwitchModLog] Subscribe {event_type} error: {e}")

    async def _post_embed(self, embed: discord.Embed) -> None:
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(self._discord_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self._discord_channel_id)
            except Exception as e:
                print(f"[TwitchModLog] Cannot fetch Discord channel: {e}")
                return
        if not isinstance(channel, discord.TextChannel):
            print("[TwitchModLog] Target is not a text channel")
            return
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TwitchModLog] Discord send failed: {e}")

    def _embed_for(self, event_type: str, event: dict[str, Any]) -> Optional[discord.Embed]:
        if event_type == "channel.ban":
            user = event.get("user_name") or event.get("user_login") or "?"
            mod = event.get("moderator_user_name") or event.get("moderator_user_login") or "?"
            reason = (event.get("reason") or "").strip() or "—"
            ends = event.get("ends_at")
            if event.get("is_permanent") or not ends:
                dur = "permanent"
            else:
                dur = f"until {ends}"
            emb = discord.Embed(
                title="🔨 Ban / Timeout",
                color=discord.Color.red(),
                description=f"**{user}** by **{mod}**",
            )
            emb.add_field(name="Duration", value=dur, inline=True)
            emb.add_field(name="Reason", value=reason[:500], inline=False)
            return emb

        if event_type == "channel.unban":
            user = event.get("user_name") or event.get("user_login") or "?"
            mod = event.get("moderator_user_name") or event.get("moderator_user_login") or "?"
            return discord.Embed(
                title="✅ Unban",
                color=discord.Color.green(),
                description=f"**{user}** by **{mod}**",
            )

        if event_type == "channel.moderator.add":
            user = event.get("user_name") or event.get("user_login") or "?"
            return discord.Embed(
                title="🛡️ Moderator added",
                color=discord.Color.blue(),
                description=f"**{user}** is now a moderator",
            )

        if event_type == "channel.moderator.remove":
            user = event.get("user_name") or event.get("user_login") or "?"
            return discord.Embed(
                title="🛡️ Moderator removed",
                color=discord.Color.dark_grey(),
                description=f"**{user}** is no longer a moderator",
            )

        if event_type == "channel.shoutout.create":
            to_name = (
                event.get("to_broadcaster_user_name")
                or event.get("to_broadcaster_user_login")
                or "?"
            )
            mod = event.get("moderator_user_name") or event.get("moderator_user_login") or "?"
            viewers = event.get("viewer_count")
            emb = discord.Embed(
                title="📢 Shoutout",
                color=discord.Color.purple(),
                description=f"**{mod}** shouted out **{to_name}**",
            )
            if viewers is not None:
                emb.add_field(name="Viewers", value=str(viewers), inline=True)
            return emb

        if event_type == "channel.raid":
            from_name = (
                event.get("from_broadcaster_user_name")
                or event.get("from_broadcaster_user_login")
                or "?"
            )
            viewers = event.get("viewers")
            emb = discord.Embed(
                title="🚀 Raid incoming",
                color=discord.Color.gold(),
                description=f"**{from_name}** is raiding the channel",
            )
            if viewers is not None:
                emb.add_field(name="Viewers", value=str(viewers), inline=True)
            return emb

        return None

    async def _handle_notification(self, payload: dict[str, Any]) -> None:
        sub = payload.get("subscription") or {}
        event = payload.get("event") or {}
        event_type = sub.get("type") or ""
        embed = self._embed_for(event_type, event)
        if embed is None:
            print(f"[TwitchModLog] Unhandled event type: {event_type}")
            return
        embed.set_footer(text=f"Twitch · #{TWITCH_CHANNEL} · {event_type}")
        await self._post_embed(embed)

    async def _run_session(self, session: Any, ws_url: str) -> None:
        async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    meta = data.get("metadata") or {}
                    mtype = meta.get("message_type")
                    payload = data.get("payload") or {}

                    if mtype == "session_welcome":
                        sess = (payload.get("session") or {})
                        session_id = sess.get("id")
                        print(f"[TwitchModLog] EventSub session {session_id}")
                        if session_id:
                            await self._subscribe_all(session, session_id)
                    elif mtype == "session_keepalive":
                        pass
                    elif mtype == "notification":
                        try:
                            await self._handle_notification(payload)
                        except Exception as e:
                            print(f"[TwitchModLog] Notify handler error: {e}")
                    elif mtype == "session_reconnect":
                        new_url = (payload.get("session") or {}).get("reconnect_url")
                        if new_url:
                            print("[TwitchModLog] Reconnect requested by Twitch")
                            await self._run_session(session, new_url)
                            return
                    elif mtype == "revocation":
                        sub = payload.get("subscription") or {}
                        print(
                            f"[TwitchModLog] Subscription revoked: "
                            f"{sub.get('type')} status={sub.get('status')}"
                        )
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    print(f"[TwitchModLog] WebSocket closed: {ws.close_code}")
                    break

    async def _run_loop(self) -> None:
        await self.bot.wait_until_ready()
        backoff = 5.0
        while True:
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=30)
                self._session = aiohttp.ClientSession(timeout=timeout)
                ok = await self._resolve_ids(self._session)
                if not ok:
                    await self._session.close()
                    self._session = None
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.5, 120.0)
                    continue

                await self._run_session(self._session, EVENTSUB_WS)
                backoff = 5.0
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TwitchModLog] Loop error: {e} – retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 120.0)
            finally:
                if self._session and not self._session.closed:
                    await self._session.close()
                self._session = None
                await asyncio.sleep(3.0)

    @app_commands.command(
        name="twitch-modlog-status",
        description="Shows Twitch mod-log EventSub status",
    )
    @app_commands.default_permissions(administrator=True)
    async def modlog_status(self, interaction: discord.Interaction) -> None:
        if not _enabled() or not self._discord_channel_id:
            await interaction.response.send_message(
                "Mod log is **not configured**. Set `TWITCH_MOD_LOG_CHANNEL_ID` "
                "(+ Twitch token / client id / channel).",
                ephemeral=True,
            )
            return

        running = bool(self._task and not self._task.done())
        embed = discord.Embed(
            title="Twitch Mod Log",
            color=discord.Color.green() if running else discord.Color.orange(),
        )
        embed.add_field(
            name="Discord channel",
            value=f"<#{self._discord_channel_id}>",
            inline=True,
        )
        embed.add_field(
            name="Twitch",
            value=f"`#{TWITCH_CHANNEL}`",
            inline=True,
        )
        embed.add_field(
            name="Worker",
            value="🟢 running" if running else "🔴 stopped",
            inline=True,
        )
        embed.add_field(
            name="IDs",
            value=(
                f"broadcaster=`{self._broadcaster_id or '?'}`\n"
                f"token user=`{self._user_id or '?'}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Events",
            value=", ".join(t for t, _, _ in SUBSCRIPTIONS),
            inline=False,
        )
        embed.set_footer(
            text="Scopes: moderator:read:banned_users, moderation:read, "
            "moderator:read:shoutouts"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TwitchModLogCog(bot))
