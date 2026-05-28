"""WebSocket transport for live trade room updates."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import WSMsgType, web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.trade_session import TradeSession
from poke_pon_bot.services.web_trades import serialize_trade_session
from poke_pon_bot.web.sessions import decode_session, read_session

_LOG = logging.getLogger(__name__)

# trade_id -> set of (websocket, viewer user id)
_ROOMS: dict[int, set[tuple[web.WebSocketResponse, int]]] = {}


async def notify_trade_room(
    session_factory: Any,
    bot: Any,
    trade_id: int,
) -> None:
    """Push a fresh trade snapshot to every socket in the room (per-viewer payload)."""
    entries = list(_ROOMS.get(int(trade_id)) or [])
    if not entries:
        return
    try:
        async with session_factory() as db:
            ts = await db.get(TradeSession, int(trade_id))
            if ts is None:
                return
            dead: list[tuple[web.WebSocketResponse, int]] = []
            for ws, uid in entries:
                try:
                    payload = await serialize_trade_session(
                        db, ts, viewer_id=int(uid), bot=bot
                    )
                    await ws.send_json({"type": "trade", "trade": payload})
                except Exception:
                    dead.append((ws, uid))
            if dead:
                room = _ROOMS.get(int(trade_id))
                if room:
                    for entry in dead:
                        room.discard(entry)
    except SQLAlchemyError:
        _LOG.exception("trade ws notify trade_id=%s", trade_id)


def register_trade_ws(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds

    async def handle_ws(request: web.Request) -> web.StreamResponse:
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            raw_tok = (request.query.get("session") or "").strip()
            if raw_tok:
                sess = decode_session(session_secret, raw_tok, max_age=session_ttl)
        if sess is None:
            raise web.HTTPUnauthorized(
                text='{"error":"unauthenticated"}',
                content_type="application/json",
            )
        uid = int(sess.user_id)
        try:
            trade_id = int(request.match_info["id"])
        except Exception:
            return web.Response(status=400, text="invalid trade id")

        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)

        _ROOMS.setdefault(trade_id, set()).add((ws, uid))

        try:
            async with session_factory() as db:
                ts = await db.get(TradeSession, trade_id)
                if ts is None or uid not in (ts.initiator_id, ts.partner_id):
                    await ws.send_json({"type": "error", "message": "not_found"})
                else:
                    payload = await serialize_trade_session(
                        db, ts, viewer_id=uid, bot=bot
                    )
                    await ws.send_json({"type": "trade", "trade": payload})
        except SQLAlchemyError:
            _LOG.exception("trade ws initial load trade_id=%s", trade_id)
            await ws.send_json({"type": "error", "message": "database_error"})

        try:
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        finally:
            room = _ROOMS.get(trade_id)
            if room:
                room.discard((ws, uid))

        return ws

    app.router.add_get("/ws/trades/{id}", handle_ws)
