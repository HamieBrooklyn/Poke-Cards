"""WebSocket transport for live duel updates and actions."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import WSMsgType, web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.duel_session import DUEL_STATUS_ACTIVE, DuelSession
from poke_pon_bot.services.web_duels import normalize_duel_currency, payout_escrow
from poke_pon_bot.services.web_duel_runtime import DuelRuntimeAdvanced
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)

# duel_id -> set(ws)
_ROOMS: dict[int, set[web.WebSocketResponse]] = {}
_ROOM_LOCKS: dict[int, asyncio.Lock] = {}


def _room_lock(duel_id: int) -> asyncio.Lock:
    lk = _ROOM_LOCKS.get(duel_id)
    if lk is None:
        lk = asyncio.Lock()
        _ROOM_LOCKS[duel_id] = lk
    return lk


async def _broadcast(duel_id: int, payload: dict[str, Any]) -> None:
    conns = list(_ROOMS.get(duel_id) or [])
    if not conns:
        return
    data = json.dumps(payload, separators=(",", ":"))
    dead: list[web.WebSocketResponse] = []
    for ws in conns:
        try:
            await ws.send_str(data)
        except Exception:
            dead.append(ws)
    if dead:
        s = _ROOMS.get(duel_id)
        if s:
            for ws in dead:
                s.discard(ws)


def register_duel_ws(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds

    async def handle_ws(request: web.Request) -> web.StreamResponse:
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            raise web.HTTPUnauthorized(
                text='{"error":"unauthenticated"}',
                content_type="application/json",
            )
        uid = int(sess.user_id)
        try:
            duel_id = int(request.match_info["id"])
        except Exception:
            return web.Response(status=400, text="invalid duel id")

        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)

        _ROOMS.setdefault(duel_id, set()).add(ws)

        async def send(payload: dict[str, Any]) -> None:
            await ws.send_json(payload)

        try:
            async with session_factory() as db:
                ds = await db.get(DuelSession, duel_id)
                if ds is None or uid not in (ds.initiator_id, ds.partner_id):
                    await send({"type": "error", "message": "not_found"})
                    return ws
                await send(
                    {
                        "type": "state",
                        "duel": {
                            "id": int(ds.id),
                            "status": ds.status,
                            "bet": {"currency": ds.bet_currency, "amount": int(ds.bet_amount or 0)},
                            "version": int(ds.version or 0),
                            "starting_player_id": int(ds.starting_player_id) if ds.starting_player_id else None,
                            "winner_id": int(ds.winner_id) if ds.winner_id else None,
                        },
                        "state": ds.state or {},
                    }
                )
        except SQLAlchemyError:
            _LOG.exception("duel ws initial load duel_id=%s", duel_id)
            await send({"type": "error", "message": "database_error"})
            return ws

        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                body = json.loads(msg.data)
            except Exception:
                await send({"type": "error", "message": "invalid_json"})
                continue
            if not isinstance(body, dict):
                await send({"type": "error", "message": "invalid_body"})
                continue

            mtype = str(body.get("type") or "").strip().lower()
            expected_version = body.get("expected_version")
            if expected_version is not None:
                try:
                    expected_version = int(expected_version)
                except Exception:
                    expected_version = None

            async with _room_lock(duel_id):
                try:
                    async with session_factory() as db:
                        ds = await db.get(DuelSession, duel_id)
                        if ds is None or uid not in (ds.initiator_id, ds.partner_id):
                            await send({"type": "error", "message": "not_found"})
                            continue
                        if ds.status != DUEL_STATUS_ACTIVE:
                            await send({"type": "error", "message": "duel_not_active"})
                            continue
                        if expected_version is not None and int(ds.version or 0) != expected_version:
                            await send({"type": "error", "message": "version_conflict", "version": int(ds.version or 0)})
                            await send({"type": "state", "duel": {"id": int(ds.id), "status": ds.status, "version": int(ds.version or 0)}, "state": ds.state or {}})
                            continue

                        rt = DuelRuntimeAdvanced.from_state(
                            ds.state or {},
                            duel_id=int(ds.id),
                            initiator_id=int(ds.initiator_id),
                            partner_id=int(ds.partner_id),
                            bet_currency=normalize_duel_currency(ds.bet_currency),
                            bet_amount=int(ds.bet_amount or 0),
                        )

                        if mtype == "draw":
                            src = body.get("from")
                            count = int(body.get("count") or 0)
                            ev = rt.apply_draw(actor_id=uid, source=str(src), count=count)
                            rt.append_log_event(ev)
                        elif mtype == "attack":
                            attacker_slot = int(body.get("attacker_slot") or 0)
                            attack_index = int(body.get("attack_index") or 0)
                            defender_slot = int(body.get("defender_slot") or 0)
                            ev = rt.apply_attack(
                                actor_id=uid,
                                attacker_slot=attacker_slot,
                                attack_index=attack_index,
                                defender_slot=defender_slot,
                            )
                            rt.append_log_event(ev)
                        elif mtype == "use_item":
                            item_id = str(body.get("item_id") or "")
                            target_slot = int(body.get("target_slot") or 0)
                            ev = rt.apply_item(actor_id=uid, item_id=item_id, target_slot=target_slot)
                            rt.append_log_event(ev)
                        elif mtype == "end_turn":
                            ev = rt.end_turn(actor_id=uid)
                            rt.append_log_event(ev)
                        elif mtype == "surrender":
                            ev = rt.surrender(actor_id=uid)
                            rt.append_log_event(ev)
                        else:
                            await send({"type": "error", "message": "unknown_type"})
                            continue

                        # persist + broadcast
                        ds.state = rt.to_state()
                        ds.version = int(ds.version or 0) + 1
                        if rt.winner_id is not None and ds.winner_id is None:
                            ds.winner_id = int(rt.winner_id)
                            ds.status = "completed"
                            await payout_escrow(db, ds, winner_id=int(rt.winner_id))
                        await db.commit()

                        payload = {
                            "type": "state",
                            "duel": {
                                "id": int(ds.id),
                                "status": ds.status,
                                "version": int(ds.version or 0),
                                "winner_id": int(ds.winner_id) if ds.winner_id else None,
                            },
                            "state": ds.state or {},
                            "event": rt.last_event,
                        }
                except ValueError as e:
                    await send({"type": "error", "message": str(e)})
                    continue
                except SQLAlchemyError:
                    _LOG.exception("duel ws action duel_id=%s type=%s", duel_id, mtype)
                    await send({"type": "error", "message": "database_error"})
                    continue

            await _broadcast(duel_id, payload)

        # disconnect
        try:
            s = _ROOMS.get(duel_id)
            if s:
                s.discard(ws)
        except Exception:
            pass
        return ws

    app.router.add_get("/ws/duels/{id}", handle_ws)

