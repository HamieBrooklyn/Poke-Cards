"""Web duel session API — invite, accept, cancel, snapshot."""

from __future__ import annotations

import json
import logging
from typing import Any

import discord
from aiohttp import web
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.duel_session import (
    DUEL_STATUS_ACTIVE,
    DUEL_STATUS_INVITED,
    DuelSession,
)
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.trade_user_search import (
    resolve_username_in_bot_guilds,
    search_members_shared_with_bot,
)
from poke_pon_bot.services.wallet import WalletService
from poke_pon_bot.services.web_duels import (
    accept_duel_invite,
    build_initial_state,
    cancel_duel,
    create_duel_invite,
    decline_duel_invite,
    expire_stale_duels,
    lock_escrow,
    normalize_duel_currency,
    surrender_duel,
)
from poke_pon_bot.web.duel_ws import broadcast_duel_state
from poke_pon_bot.web.sessions import read_session
from poke_pon_bot.web.user_profiles import resolve_user_profiles

_LOG = logging.getLogger(__name__)

_LIVE_STATUSES = (DUEL_STATUS_INVITED, DUEL_STATUS_ACTIVE)


def register_duel_api(app: web.Application, *, bot: Any, settings: Any) -> None:
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

    async def handle_user_search(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        q = (request.query.get("q") or "").strip().lstrip("@").strip()
        try:
            limit = int(request.query.get("limit") or 15)
        except ValueError:
            limit = 15
        try:
            users = await search_members_shared_with_bot(
                bot,
                query=q,
                requester_id=uid,
                limit=limit,
                session_factory=session_factory,
            )
        except Exception:
            _LOG.exception("duel user search q=%r", q)
            return web.json_response({"error": "search_failed"}, status=500)
        return web.json_response({"users": users})

    # POST /api/me/duels — create invite
    async def handle_create(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)

        partner_id_raw = body.get("partner_id")
        partner_username = body.get("partner_username")
        bet_currency = normalize_duel_currency(body.get("bet_currency"))
        bet_amount = int(body.get("bet_amount") or 0)

        try:
            await expire_stale_duels(session_factory)
        except Exception:
            pass

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
                        session_factory=session_factory,
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

                result = await create_duel_invite(
                    db,
                    initiator_id=uid,
                    partner_id=partner_id,
                    bet_currency=bet_currency,
                    bet_amount=bet_amount,
                )
                if isinstance(result, str):
                    return web.json_response({"error": "duel_error", "message": result}, status=400)
                await db.commit()

                payload = await serialize_duel_session(db, result, viewer_id=uid, bot=bot)
        except SQLAlchemyError:
            _LOG.exception("duel create uid=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)

        duel_link = f"{public_url}/duel/" if public_url else ""
        init_name = sess.global_name or sess.username or str(uid)
        await _try_dm(
            partner_id,
            f"**{init_name}** wants to duel you on the website!"
            + (f"\nOpen duels: {duel_link}" if duel_link else ""),
        )
        return web.json_response(payload, status=201)

    async def handle_list(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            await expire_stale_duels(session_factory)
        except Exception:
            pass

        try:
            async with session_factory() as db:
                rows = await db.execute(
                    select(DuelSession)
                    .where(
                        DuelSession.status.in_(_LIVE_STATUSES),
                        or_(DuelSession.initiator_id == uid, DuelSession.partner_id == uid),
                    )
                    .order_by(DuelSession.created_at.desc())
                    .limit(20)
                )
                out = [
                    await serialize_duel_session(db, ds, viewer_id=uid, bot=bot)
                    for ds in rows.scalars()
                ]
        except SQLAlchemyError:
            _LOG.exception("duel list uid=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"duels": out})

    async def handle_detail(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            did = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)
        try:
            async with session_factory() as db:
                ds = await db.get(DuelSession, did)
                if ds is None:
                    return web.json_response({"error": "not_found"}, status=404)
                if uid not in (ds.initiator_id, ds.partner_id):
                    return web.json_response({"error": "not_found"}, status=404)
                payload = await serialize_duel_session(db, ds, viewer_id=uid, bot=bot, include_state=True)
        except SQLAlchemyError:
            _LOG.exception("duel detail did=%s", did)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(payload)

    async def handle_accept(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            did = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)
        try:
            async with session_factory() as db:
                err = await accept_duel_invite(db, duel_id=did, user_id=uid)
                if err:
                    return web.json_response({"error": "duel_error", "message": err}, status=400)
                ds = await db.get(DuelSession, did)
                if ds is None:
                    return web.json_response({"error": "not_found"}, status=404)

                # Lock stake + initialize state at accept.
                err2 = await lock_escrow(db, row=ds, wallet=wallet, crystals=crystals)
                if err2:
                    return web.json_response({"error": "duel_error", "message": err2}, status=400)
                if not ds.state:
                    init_state = await build_initial_state(
                        db,
                        duel_id=ds.id,
                        initiator_id=ds.initiator_id,
                        partner_id=ds.partner_id,
                    )
                    if isinstance(init_state, str):
                        return web.json_response({"error": "duel_error", "message": init_state}, status=400)
                    ds.state = init_state
                    ds.starting_player_id = int(init_state["turn"])
                    ds.version = int(ds.version or 0) + 1
                await db.commit()
                ds = await db.get(DuelSession, did)
                if ds is None:
                    return web.json_response({"error": "not_found"}, status=404)
                await broadcast_duel_state(did, ds)
                payload = await serialize_duel_session(
                    db, ds, viewer_id=uid, bot=bot, include_state=True
                )
        except SQLAlchemyError:
            _LOG.exception("duel accept did=%s", did)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(payload)

    async def handle_decline(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            did = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)
        try:
            async with session_factory() as db:
                err = await decline_duel_invite(db, duel_id=did, user_id=uid)
                if err:
                    return web.json_response({"error": "duel_error", "message": err}, status=400)
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("duel decline did=%s", did)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True})

    async def handle_cancel(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            did = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)
        try:
            async with session_factory() as db:
                err = await cancel_duel(db, duel_id=did, user_id=uid)
                if err:
                    return web.json_response({"error": "duel_error", "message": err}, status=400)
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("duel cancel did=%s", did)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True})

    async def handle_surrender(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            did = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)
        try:
            async with session_factory() as db:
                err = await surrender_duel(db, duel_id=did, user_id=uid)
                if err:
                    return web.json_response({"error": "duel_error", "message": err}, status=400)
                ds = await db.get(DuelSession, did)
                if ds and ds.winner_id:
                    from poke_pon_bot.services.web_duels import payout_escrow

                    await payout_escrow(db, ds, winner_id=int(ds.winner_id))
                await db.commit()
                ds = await db.get(DuelSession, did)
                if ds is None:
                    return web.json_response({"error": "not_found"}, status=404)
                await broadcast_duel_state(did, ds)
                payload = await serialize_duel_session(
                    db, ds, viewer_id=uid, bot=bot, include_state=True
                )
        except SQLAlchemyError:
            _LOG.exception("duel surrender did=%s", did)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(payload)

    app.router.add_get("/api/me/duel-user-search", handle_user_search)
    app.router.add_post("/api/me/duels", handle_create)
    app.router.add_get("/api/me/duels", handle_list)
    app.router.add_get("/api/me/duels/{id}", handle_detail)
    app.router.add_post("/api/me/duels/{id}/accept", handle_accept)
    app.router.add_post("/api/me/duels/{id}/decline", handle_decline)
    app.router.add_post("/api/me/duels/{id}/cancel", handle_cancel)
    app.router.add_post("/api/me/duels/{id}/surrender", handle_surrender)


async def serialize_duel_session(
    db, ds: DuelSession, *, viewer_id: int, bot: Any, include_state: bool = False
) -> dict[str, Any]:
    ids = [int(ds.initiator_id), int(ds.partner_id)]
    profiles = await resolve_user_profiles(db, bot, ids)
    out: dict[str, Any] = {
        "id": int(ds.id),
        "status": ds.status,
        "initiator": profiles.get(int(ds.initiator_id)),
        "partner": profiles.get(int(ds.partner_id)),
        "bet": {
            "currency": ds.bet_currency,
            "amount": int(ds.bet_amount or 0),
        },
        "escrow": {
            "currency": ds.escrow_currency,
            "amount_each": int(ds.escrow_amount_each or 0),
            "locked_at": ds.escrow_locked_at.isoformat() if ds.escrow_locked_at else None,
            "paid_out_at": ds.escrow_paid_out_at.isoformat() if ds.escrow_paid_out_at else None,
        },
        "starting_player_id": int(ds.starting_player_id) if ds.starting_player_id else None,
        "winner_id": int(ds.winner_id) if ds.winner_id else None,
        "version": int(ds.version or 0),
        "created_at": ds.created_at.isoformat() if ds.created_at else None,
        "expires_at": ds.expires_at.isoformat() if ds.expires_at else None,
        "updated_at": ds.updated_at.isoformat() if ds.updated_at else None,
        "viewer_id": int(viewer_id),
    }
    if include_state:
        out["state"] = ds.state or {}
    return out

