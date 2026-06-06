"""Notification inbox API for the website."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.user_notifications import (
    list_notifications,
    mark_notifications_read,
    serialize_notification,
    unread_count,
)
from poke_pon_bot.services.web_preferences import (
    get_or_create_web_preferences,
    serialize_web_preferences,
)
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def register_notifications_api(app: web.Application, *, bot: Any, settings: Any) -> None:
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

    async def handle_summary(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            async with session_factory() as session:
                count = await unread_count(session, uid)
                prefs = await get_or_create_web_preferences(session, uid)
                settings_payload = serialize_web_preferences(prefs)
        except SQLAlchemyError:
            _LOG.exception("GET /api/me/notifications/summary user=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(
            {
                "unread_count": count,
                "notify_browser": bool(settings_payload.get("notify_browser")),
            }
        )

    async def handle_list(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        unread_only = (request.query.get("unread_only") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        try:
            limit = int(request.query.get("limit") or "40")
        except ValueError:
            limit = 40
        try:
            async with session_factory() as session:
                rows = await list_notifications(
                    session, uid, limit=limit, unread_only=unread_only
                )
        except SQLAlchemyError:
            _LOG.exception("GET /api/me/notifications user=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(
            {"notifications": [serialize_notification(r) for r in rows]}
        )

    async def handle_mark_read(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_body"}, status=400)
        mark_all = bool(body.get("all"))
        ids_raw = body.get("ids")
        ids: list[int] = []
        if isinstance(ids_raw, list):
            for raw in ids_raw:
                try:
                    ids.append(int(raw))
                except (TypeError, ValueError):
                    continue
        if not mark_all and not ids:
            return web.json_response({"error": "ids_or_all_required"}, status=400)
        try:
            async with session_factory() as session:
                marked = await mark_notifications_read(
                    session, uid, notification_ids=ids, mark_all=mark_all
                )
                remaining = await unread_count(session, uid)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("POST /api/me/notifications/read user=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True, "marked": marked, "unread_count": remaining})

    app.router.add_get("/api/me/notifications/summary", handle_summary)
    app.router.add_get("/api/me/notifications", handle_list)
    app.router.add_post("/api/me/notifications/read", handle_mark_read)
