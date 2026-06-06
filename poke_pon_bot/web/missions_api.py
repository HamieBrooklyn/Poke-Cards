"""Authenticated missions API for the website."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.crystals import CrystalsService, format_crystals
from poke_pon_bot.services.missions import MissionService
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def register_missions_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds
    crystals = CrystalsService()
    missions = MissionService()

    def _require_session(request: web.Request):
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            raise web.HTTPUnauthorized(
                text='{"error":"unauthenticated"}',
                content_type="application/json",
            )
        return sess

    async def handle_list(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            async with session_factory() as session:
                rows = await missions.list_active_missions(session, uid)
                balance = await crystals.get_balance(session, uid)
                payload = missions.missions_board_to_public_json(
                    rows,
                    crystal_balance=balance,
                )
        except SQLAlchemyError:
            _LOG.exception("GET /api/me/missions user=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(payload)

    async def handle_claim(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            mission_id = int(request.match_info["id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "invalid_mission_id"}, status=400)

        try:
            async with session_factory() as session:
                mission, err = await missions.claim_mission(
                    session,
                    crystals,
                    discord_user_id=uid,
                    mission_id=mission_id,
                )
                if err:
                    return web.json_response({"error": err}, status=400)
                balance = await crystals.get_balance(session, uid)
                refreshed = await missions.list_active_missions(session, uid)
                board = missions.missions_board_to_public_json(
                    refreshed,
                    crystal_balance=balance,
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("POST /api/me/missions/%s/claim user=%s", mission_id, uid)
            return web.json_response({"error": "database_error"}, status=500)

        assert mission is not None
        return web.json_response(
            {
                "ok": True,
                "claimed_crystals": int(mission.reward_crystals),
                "crystal_balance": int(balance),
                "crystal_balance_formatted": format_crystals(balance),
                "missions": board,
            }
        )

    app.router.add_get("/api/me/missions", handle_list)
    app.router.add_post(r"/api/me/missions/{id:\d+}/claim", handle_claim)
