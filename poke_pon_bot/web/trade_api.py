"""Web trade session API — invite, accept, update, ready, cancel."""

from __future__ import annotations

import json
import logging
from typing import Any

import discord
from aiohttp import web
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.known_user import KnownUser
from poke_pon_bot.services.trade_user_search import (
    resolve_username_in_bot_guilds,
    search_members_shared_with_bot,
)
from poke_pon_bot.models.trade_session import (
    TRADE_STATUS_ACTIVE,
    TRADE_STATUS_INVITED,
    TradeSession,
)
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.wallet import WalletService
from poke_pon_bot.services.web_trades import (
    accept_trade_invite,
    cancel_trade,
    create_trade_invite,
    decline_trade_invite,
    expire_stale_sessions,
    serialize_trade_session,
    toggle_ready,
    update_trade_side,
)
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)

_LIVE_STATUSES = (TRADE_STATUS_INVITED, TRADE_STATUS_ACTIVE)


def register_trade_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds
    wallet = WalletService()
    crystals = CrystalsService()
    public_url = (settings.web_frontend_url or "").rstrip("/")

    def _require_session(request: web.Request):
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            raise web.HTTPUnauthorized(
                text='{"error":"unauthenticated"}',
                content_type="application/json",
            )
        return sess

    async def _try_dm(user_id: int, content: str) -> None:
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            await user.send(content)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass
        except Exception:
            _LOG.debug("DM to %s failed", user_id)

    # POST /api/me/trades — create invite
    async def handle_create(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)

        partner_id_raw = body.get("partner_id")
        partner_username = body.get("partner_username")

        try:
            await expire_stale_sessions(session_factory)
        except Exception:
            _LOG.exception("expire stale sessions")

        try:
            async with session_factory() as db:
                partner_id: int | None = None
                if partner_id_raw:
                    try:
                        partner_id = int(str(partner_id_raw).strip())
                    except (ValueError, TypeError):
                        return web.json_response({"error": "invalid_partner_id"}, status=400)
                elif partner_username:
                    resolved = await resolve_username_in_bot_guilds(
                        bot,
                        username=str(partner_username),
                        requester_id=uid,
                    )
                    if resolved is None:
                        return web.json_response(
                            {
                                "error": "user_not_found",
                                "message": "No Discord user with that exact username found in any server with this bot. "
                                "Use the search suggestions, or paste their numeric User ID.",
                            },
                            status=404,
                        )
                    partner_id = resolved
                else:
                    return web.json_response({"error": "missing_partner"}, status=400)

                result = await create_trade_invite(db, initiator_id=uid, partner_id=partner_id)
                if isinstance(result, str):
                    return web.json_response({"error": "trade_error", "message": result}, status=400)
                await db.commit()
                payload = await serialize_trade_session(db, result, viewer_id=uid)
        except SQLAlchemyError:
            _LOG.exception("trade create uid=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)

        trade_link = f"{public_url}/trades.html" if public_url else ""
        init_name = sess.global_name or sess.username or str(uid)
        await _try_dm(
            partner_id,
            f"**{init_name}** wants to trade with you!"
            + (f"\nOpen the trade on the website: {trade_link}" if trade_link else ""),
        )
        return web.json_response(payload, status=201)

    # GET /api/me/trades — list user's sessions
    async def handle_list(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)

        try:
            await expire_stale_sessions(session_factory)
        except Exception:
            pass

        try:
            async with session_factory() as db:
                rows = await db.execute(
                    select(TradeSession)
                    .where(
                        TradeSession.status.in_(_LIVE_STATUSES),
                        or_(TradeSession.initiator_id == uid, TradeSession.partner_id == uid),
                    )
                    .order_by(TradeSession.created_at.desc())
                    .limit(20)
                )
                out = []
                for ts in rows.scalars():
                    out.append(await serialize_trade_session(db, ts, viewer_id=uid))
        except SQLAlchemyError:
            _LOG.exception("trade list uid=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"trades": out})

    # GET /api/me/trades/{id} — trade detail (polling endpoint)
    async def handle_detail(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            tid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)

        try:
            async with session_factory() as db:
                ts = await db.get(TradeSession, tid)
                if ts is None:
                    return web.json_response({"error": "not_found"}, status=404)
                if uid not in (ts.initiator_id, ts.partner_id):
                    return web.json_response({"error": "not_found"}, status=404)
                payload = await serialize_trade_session(db, ts, viewer_id=uid)
        except SQLAlchemyError:
            _LOG.exception("trade detail tid=%s", tid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(payload)

    # POST /api/me/trades/{id}/accept
    async def handle_accept(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            tid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)

        try:
            async with session_factory() as db:
                err = await accept_trade_invite(db, trade_id=tid, user_id=uid)
                if err:
                    return web.json_response({"error": "trade_error", "message": err}, status=400)
                ts = await db.get(TradeSession, tid)
                await db.commit()
                partner_name = sess.global_name or sess.username or str(uid)
                if ts:
                    await _try_dm(ts.initiator_id, f"**{partner_name}** accepted your trade invite! Head to the website to start adding cards.")
        except SQLAlchemyError:
            _LOG.exception("trade accept tid=%s", tid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True})

    # POST /api/me/trades/{id}/decline
    async def handle_decline(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            tid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)

        try:
            async with session_factory() as db:
                ts = await db.get(TradeSession, tid)
                err = await decline_trade_invite(db, trade_id=tid, user_id=uid)
                if err:
                    return web.json_response({"error": "trade_error", "message": err}, status=400)
                await db.commit()
                if ts:
                    partner_name = sess.global_name or sess.username or str(uid)
                    await _try_dm(ts.initiator_id, f"**{partner_name}** declined your trade invite.")
        except SQLAlchemyError:
            _LOG.exception("trade decline tid=%s", tid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True})

    # POST /api/me/trades/{id}/update — update caller's side
    async def handle_update(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            tid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)

        card_ids = body.get("card_ids", [])
        if not isinstance(card_ids, list):
            return web.json_response({"error": "invalid_card_ids"}, status=400)
        try:
            card_ids = [int(c) for c in card_ids]
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid_card_ids"}, status=400)

        pd = int(body.get("pokedollars", 0) or 0)
        cr = int(body.get("crystals", 0) or 0)

        try:
            async with session_factory() as db:
                err = await update_trade_side(
                    db, trade_id=tid, user_id=uid,
                    card_ids=card_ids, pokedollars=pd, crystals=cr,
                )
                if err:
                    return web.json_response({"error": "trade_error", "message": err}, status=400)
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("trade update tid=%s", tid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True})

    # POST /api/me/trades/{id}/ready — toggle ready
    async def handle_ready(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            tid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)

        try:
            async with session_factory() as db:
                err, completed = await toggle_ready(
                    db, wallet, crystals, trade_id=tid, user_id=uid,
                )
                if err:
                    return web.json_response({"error": "trade_error", "message": err}, status=400)
                ts = await db.get(TradeSession, tid)
                await db.commit()

                if completed and ts:
                    init_name = (await db.get(KnownUser, ts.initiator_id))
                    part_name = (await db.get(KnownUser, ts.partner_id))
                    i_label = init_name.global_name or init_name.username if init_name else str(ts.initiator_id)
                    p_label = part_name.global_name or part_name.username if part_name else str(ts.partner_id)
                    await _try_dm(ts.initiator_id, f"Trade with **{p_label}** completed! Check your collection.")
                    await _try_dm(ts.partner_id, f"Trade with **{i_label}** completed! Check your collection.")
        except SQLAlchemyError:
            _LOG.exception("trade ready tid=%s", tid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True, "completed": completed})

    # POST /api/me/trades/{id}/cancel
    async def handle_cancel(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            tid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)

        try:
            async with session_factory() as db:
                ts = await db.get(TradeSession, tid)
                err = await cancel_trade(db, trade_id=tid, user_id=uid)
                if err:
                    return web.json_response({"error": "trade_error", "message": err}, status=400)
                await db.commit()
                if ts:
                    other_id = ts.partner_id if uid == ts.initiator_id else ts.initiator_id
                    canceller_name = sess.global_name or sess.username or str(uid)
                    await _try_dm(other_id, f"**{canceller_name}** cancelled the trade.")
        except SQLAlchemyError:
            _LOG.exception("trade cancel tid=%s", tid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True})

    # GET /api/me/trades/pending-count — notification badge
    async def handle_pending_count(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            async with session_factory() as db:
                n = await db.scalar(
                    select(func.count(TradeSession.id)).where(
                        TradeSession.status == TRADE_STATUS_INVITED,
                        TradeSession.partner_id == uid,
                    )
                )
        except SQLAlchemyError:
            n = 0
        return web.json_response({"count": int(n or 0)})

    async def handle_trade_user_search(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        q = (request.query.get("q") or "").strip()
        try:
            lim = int(request.query.get("limit", "15"))
        except ValueError:
            lim = 15
        try:
            users = await search_members_shared_with_bot(
                bot, query=q, requester_id=uid, limit=lim
            )
        except Exception:
            _LOG.exception("trade user search q=%r", q)
            return web.json_response({"error": "search_failed"}, status=500)
        return web.json_response({"users": users})

    app.router.add_get("/api/me/trade-user-search", handle_trade_user_search)
    app.router.add_post("/api/me/trades", handle_create)
    app.router.add_get("/api/me/trades", handle_list)
    app.router.add_get("/api/me/trades/pending-count", handle_pending_count)
    app.router.add_get(r"/api/me/trades/{id:\d+}", handle_detail)
    app.router.add_post(r"/api/me/trades/{id:\d+}/accept", handle_accept)
    app.router.add_post(r"/api/me/trades/{id:\d+}/decline", handle_decline)
    app.router.add_post(r"/api/me/trades/{id:\d+}/update", handle_update)
    app.router.add_post(r"/api/me/trades/{id:\d+}/ready", handle_ready)
    app.router.add_post(r"/api/me/trades/{id:\d+}/cancel", handle_cancel)
