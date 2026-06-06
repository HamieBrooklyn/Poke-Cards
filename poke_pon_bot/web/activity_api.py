"""Authenticated activity feed for the website."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.activity_feed import fetch_user_activity_feed
from poke_pon_bot.web.sessions import read_session
from poke_pon_bot.web.user_profiles import resolve_user_profiles

_LOG = logging.getLogger(__name__)


def register_activity_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds

    def _require_session(request: web.Request):
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            raise web.HTTPUnauthorized(
                text='{"error":"unauthenticated"}',
                content_type="application/json",
            )
        return sess

    async def handle_activity(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            limit = int(request.query.get("limit") or "50")
        except ValueError:
            limit = 50

        try:
            async with session_factory() as session:
                payload = await fetch_user_activity_feed(
                    session,
                    uid,
                    limit=limit,
                    weekend_luck_enabled=settings.weekend_luck_enabled,
                    weekend_luck_percent=settings.weekend_luck_percent,
                    weekend_luck_timezone=settings.weekend_luck_timezone,
                )
                profile_ids: set[int] = set()
                for item in payload["items"]:
                    if item["kind"] == "trade":
                        raw = item.get("partner_discord_id")
                        if raw and str(raw).isdigit():
                            profile_ids.add(int(raw))
                    elif item["kind"] in ("auction_won", "auction_sold"):
                        raw = item.get("counterparty_discord_id")
                        if raw and str(raw).isdigit() and int(raw) > 0:
                            profile_ids.add(int(raw))
                profiles: dict[str, Any] = {}
                if profile_ids:
                    resolved = await resolve_user_profiles(session, bot, profile_ids)
                    profiles = {
                        str(pid): prof for pid, prof in resolved.items()
                    }
        except SQLAlchemyError:
            _LOG.exception("GET /api/me/activity user=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response({"profiles": profiles, **payload})

    app.router.add_get("/api/me/activity", handle_activity)
