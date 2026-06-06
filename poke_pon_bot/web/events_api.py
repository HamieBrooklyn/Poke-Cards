"""Public events JSON for the website home page."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from poke_pon_bot.services.discord_events import fetch_public_discord_events
from poke_pon_bot.services.event_scheduler import list_events_public
from poke_pon_bot.services.set_chase import build_status, season_to_public_json
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def register_events_public_api(app: web.Application, *, bot: Any) -> None:
    settings = bot.settings
    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds

    async def handle_events(request: web.Request) -> web.StreamResponse:
        try:
            events = await fetch_public_discord_events(bot, settings)
        except Exception:
            _LOG.exception("GET /api/events failed")
            cached = getattr(bot, "_pokepon_events_cache", None)
            events = cached[1] if cached else []

        set_chase_payload = None
        game_events: list[dict[str, Any]] = []
        try:
            async with session_factory() as session:
                sess = read_session(request, session_secret, max_age=session_ttl)
                user_id = int(sess.user_id) if sess is not None else None
                game_events = await list_events_public(session, limit=12)
                status = await build_status(session, discord_user_id=user_id)
                if status is not None:
                    personal = None
                    if status.personal is not None:
                        personal = {
                            "owned_unique": status.personal.owned_unique,
                            "total_unique": status.personal.total_unique,
                            "percent": round(status.personal.percent, 2),
                            "reward_claimed": status.reward_claimed,
                            "reward_eligible": status.reward_eligible,
                            "participated": status.user_participated,
                            "community_reward_paid": status.user_community_reward_paid,
                        }
                    set_chase_payload = season_to_public_json(status, personal=personal)
        except Exception:
            _LOG.exception("GET /api/events set_chase failed")

        return web.json_response(
            {"events": events, "set_chase": set_chase_payload, "game_events": game_events}
        )

    async def handle_set_chase_claim(request: web.Request) -> web.StreamResponse:
        if not session_secret:
            return web.json_response({"error": "auth_disabled"}, status=503)
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        from poke_pon_bot.services.set_chase import claim_personal_reward

        try:
            async with session_factory() as session:
                season, err = await claim_personal_reward(
                    session, discord_user_id=int(sess.user_id)
                )
                if err:
                    return web.json_response({"error": "claim_failed", "message": err}, status=400)
                await session.commit()
                status = await build_status(session, discord_user_id=int(sess.user_id))
                personal = None
                if status and status.personal is not None:
                    personal = {
                        "owned_unique": status.personal.owned_unique,
                        "total_unique": status.personal.total_unique,
                        "percent": round(status.personal.percent, 2),
                        "reward_claimed": status.reward_claimed,
                        "reward_eligible": status.reward_eligible,
                    }
                payload = (
                    season_to_public_json(status, personal=personal)
                    if status is not None
                    else None
                )
                return web.json_response(
                    {
                        "ok": True,
                        "set_chase": payload,
                        "reward": {
                            "crystals": int(season.reward_crystals) if season else 0,
                            "pokedollars": int(season.reward_pokedollars) if season else 0,
                        },
                    }
                )
        except Exception:
            _LOG.exception("POST /api/me/set-chase/claim failed")
            return web.json_response({"error": "database_error"}, status=500)

    app.router.add_get("/api/events", handle_events)
    app.router.add_post("/api/me/set-chase/claim", handle_set_chase_claim)
