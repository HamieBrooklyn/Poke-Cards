"""Public catalog news for the website home page."""

from __future__ import annotations

import logging

from aiohttp import web

from poke_pon_bot.services.catalog_news import get_news_public, list_news_public

_LOG = logging.getLogger(__name__)


def register_news_public_api(app: web.Application, *, bot) -> None:
    session_factory = bot.async_session_factory

    async def handle_news(request: web.Request) -> web.StreamResponse:
        try:
            limit = int(request.rel_url.query.get("limit") or "12")
        except ValueError:
            limit = 12
        try:
            async with session_factory() as session:
                items = await list_news_public(session, limit=limit)
        except Exception:
            _LOG.exception("GET /api/news failed")
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"items": items})

    async def handle_news_detail(request: web.Request) -> web.StreamResponse:
        raw_id = request.match_info.get("news_id", "").strip()
        try:
            news_id = int(raw_id)
        except ValueError:
            return web.json_response({"error": "invalid_news_id"}, status=400)
        try:
            async with session_factory() as session:
                item = await get_news_public(session, announcement_id=news_id)
        except Exception:
            _LOG.exception("GET /api/news/%s failed", news_id)
            return web.json_response({"error": "database_error"}, status=500)
        if item is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(item)

    app.router.add_get("/api/news", handle_news)
    app.router.add_get("/api/news/{news_id}", handle_news_detail)
    _LOG.info("News public API mounted: GET /api/news, GET /api/news/{news_id}")
