"""Public events JSON for the website home page."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from poke_pon_bot.services.discord_events import fetch_public_discord_events

_LOG = logging.getLogger(__name__)


def register_events_public_api(app: web.Application, *, bot: Any) -> None:
    async def handle_events(request: web.Request) -> web.StreamResponse:
        del request
        settings = bot.settings
        try:
            events = await fetch_public_discord_events(bot, settings)
        except Exception:
            _LOG.exception("GET /api/events failed")
            cached = getattr(bot, "_pokepon_events_cache", None)
            events = cached[1] if cached else []

        return web.json_response({"events": events})

    app.router.add_get("/api/events", handle_events)
