"""Guild milestone status and the user's shared servers for server-scoped leaderboards."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.trade_user_search import list_shared_guilds_for_user

from poke_pon_bot.services.guild_milestones import (
    get_milestone_status,
    get_settings,
    serialize_milestone_status,
    serialize_settings,
)
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def register_guild_milestones_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds

    async def handle_guild_milestones(request: web.Request) -> web.StreamResponse:
        guild_raw = (request.query.get("guild_id") or "").strip()
        if not guild_raw.isdigit():
            return web.json_response({"error": "guild_id_required"}, status=400)
        guild_id = int(guild_raw)
        guild = bot.get_guild(guild_id)
        guild_name = guild.name if guild is not None else f"Server {guild_id}"

        try:
            async with session_factory() as db:
                status = await get_milestone_status(db, guild_id)
                cfg = await get_settings(db, guild_id)
        except SQLAlchemyError:
            _LOG.exception("guild milestones guild_id=%s", guild_id)
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response(
            {
                "guild_id": str(guild_id),
                "guild_name": guild_name,
                "milestones": serialize_milestone_status(status),
                "settings": serialize_settings(cfg),
            }
        )

    async def handle_me_guilds(request: web.Request) -> web.StreamResponse:
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            return web.json_response({"authenticated": False, "guilds": []})

        uid = int(sess.user_id)
        try:
            guilds = await list_shared_guilds_for_user(bot, uid)
        except SQLAlchemyError:
            _LOG.exception("me guilds user=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        except (RuntimeError, OSError) as exc:
            _LOG.exception("me guilds discord user=%s: %s", uid, exc)
            return web.json_response({"error": "discord_unavailable"}, status=503)

        return web.json_response({"authenticated": True, "guilds": guilds})

    app.router.add_get("/api/guild-milestones", handle_guild_milestones)
    app.router.add_get("/api/me/guilds", handle_me_guilds)
