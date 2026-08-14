"""Public market quotes + signed-in invest/sell."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.card_market import (
    INVEST_MAX_PD,
    INVEST_MIN_PD,
    InvestmentNotFoundError,
    MarketUnavailableError,
    NotInCollectionError,
    _position_view,
    buy_position,
    get_open_position,
    list_positions,
    load_history,
    load_quote,
    position_payload,
    quote_payload,
    sell_position,
    user_owns_catalog_card,
)
from poke_pon_bot.services.excluded_sets import excluded_set_clause
from poke_pon_bot.services.wallet import InsufficientPokedollarsError, WalletService
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def register_market_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    session_factory = bot.async_session_factory
    session_secret = getattr(settings, "web_session_secret", None)
    session_ttl = int(getattr(settings, "web_session_ttl_seconds", 0) or 0) or 60 * 60 * 24 * 30
    api_key = getattr(settings, "tcg_api_key", None)
    wallet = WalletService()

    def _optional_user(request: web.Request) -> int | None:
        if not session_secret:
            return None
        sess = read_session(request, session_secret, max_age=session_ttl)
        return sess.user_id if sess else None

    def _require_user(request: web.Request) -> int:
        uid = _optional_user(request)
        if uid is None:
            raise web.HTTPUnauthorized(
                text='{"error":"unauthenticated"}',
                content_type="application/json",
            )
        return uid

    async def handle_quote(request: web.Request) -> web.StreamResponse:
        try:
            card_id = int(request.match_info.get("card_id", ""))
        except ValueError:
            return web.json_response({"error": "invalid_card_id"}, status=400)
        try:
            days = max(7, min(int(request.query.get("days") or 90), 365))
        except ValueError:
            days = 90
        try:
            async with session_factory() as db:
                row = (
                    await db.execute(
                        select(Card, RarityClass)
                        .join(RarityClass, Card.rarity_class_id == RarityClass.id, isouter=True)
                        .where(Card.id == card_id, excluded_set_clause(Card.set_code))
                    )
                ).one_or_none()
                if row is None:
                    return web.json_response({"error": "not_found"}, status=404)
                card, rarity = row
                quote = await load_quote(db, card, api_key=api_key, allow_stale=True)
                hist = await load_history(db, int(card.id), days=days)
                await db.commit()
        except MarketUnavailableError as exc:
            return web.json_response({"error": "no_market", "message": str(exc)}, status=404)
        except SQLAlchemyError:
            _LOG.exception("market quote card=%s", card_id)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(
            {
                "card": {
                    "id": int(card.id),
                    "tcg_card_id": card.tcg_card_id,
                    "name": card.name,
                    "set_code": card.set_code,
                    "set_name": card.set_name,
                    "collector_number": card.collector_number,
                    "image_small_url": card.image_small_url,
                    "image_large_url": card.image_large_url,
                    "tcg_rarity": card.tcg_rarity,
                    "rarity": {
                        "display_name": rarity.display_name if rarity else None,
                    },
                },
                "quote": quote_payload(quote),
                "history": [{"day": p.day.isoformat(), "usd": p.usd_cents / 100.0} for p in hist],
                "invest_min": INVEST_MIN_PD,
                "invest_max": INVEST_MAX_PD,
            }
        )

    async def handle_my_positions(request: web.Request) -> web.StreamResponse:
        uid = _require_user(request)
        try:
            async with session_factory() as db:
                views = await list_positions(db, discord_user_id=uid, api_key=api_key)
                bal = await wallet.get_balance(db, uid)
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("market positions user=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(
            {
                "items": [position_payload(v) for v in views],
                "balance_pokedollars": bal,
            }
        )

    async def handle_buy(request: web.Request) -> web.StreamResponse:
        uid = _require_user(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        try:
            card_id = int(body.get("card_id"))
            amount = int(body.get("amount"))
        except (TypeError, ValueError):
            return web.json_response({"error": "card_id and amount required"}, status=400)
        try:
            async with session_factory() as db:
                card = await db.get(Card, card_id)
                if card is None:
                    return web.json_response({"error": "not_found"}, status=404)
                view = await buy_position(
                    db,
                    discord_user_id=uid,
                    card=card,
                    amount=amount,
                    api_key=api_key,
                    wallet=wallet,
                )
                bal = await wallet.get_balance(db, uid)
                await db.commit()
        except ValueError as exc:
            return web.json_response({"error": "invalid_amount", "message": str(exc)}, status=400)
        except InsufficientPokedollarsError:
            return web.json_response({"error": "insufficient_pokedollars"}, status=400)
        except NotInCollectionError:
            return web.json_response(
                {"error": "not_in_collection", "message": "You can only invest in cards you own."},
                status=403,
            )
        except MarketUnavailableError as exc:
            return web.json_response({"error": "no_market", "message": str(exc)}, status=404)
        except SQLAlchemyError:
            _LOG.exception("market buy user=%s card=%s", uid, card_id)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True, "position": position_payload(view), "balance_pokedollars": bal})

    async def handle_sell(request: web.Request) -> web.StreamResponse:
        uid = _require_user(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        try:
            investment_id = int(body.get("investment_id"))
        except (TypeError, ValueError):
            return web.json_response({"error": "investment_id required"}, status=400)
        try:
            async with session_factory() as db:
                view, bal = await sell_position(
                    db,
                    discord_user_id=uid,
                    investment_id=investment_id,
                    api_key=api_key,
                    wallet=wallet,
                )
                await db.commit()
        except InvestmentNotFoundError:
            return web.json_response({"error": "not_found"}, status=404)
        except MarketUnavailableError as exc:
            return web.json_response({"error": "no_market", "message": str(exc)}, status=404)
        except SQLAlchemyError:
            _LOG.exception("market sell user=%s inv=%s", uid, investment_id)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(
            {
                "ok": True,
                "sold": position_payload(view),
                "balance_pokedollars": bal,
            }
        )

    async def handle_owned_card_market(request: web.Request) -> web.StreamResponse:
        uid = _require_user(request)
        try:
            card_id = int(request.match_info.get("card_id", ""))
        except ValueError:
            return web.json_response({"error": "invalid_card_id"}, status=400)
        try:
            async with session_factory() as db:
                card = await db.get(Card, card_id)
                if card is None:
                    return web.json_response({"error": "not_found"}, status=404)
                if not await user_owns_catalog_card(
                    db, discord_user_id=uid, card_id=card_id
                ):
                    return web.json_response(
                        {
                            "error": "not_in_collection",
                            "message": "You can only view market for cards you own.",
                        },
                        status=403,
                    )
                quote = await load_quote(db, card, api_key=api_key, allow_stale=True)
                hist = await load_history(db, card_id, days=90)
                inv = await get_open_position(db, discord_user_id=uid, card_id=card_id)
                bal = await wallet.get_balance(db, uid)
                position = None
                if inv is not None:
                    position = position_payload(_position_view(inv, card, quote))
                await db.commit()
        except MarketUnavailableError as exc:
            return web.json_response({"error": "no_market", "message": str(exc)}, status=404)
        except SQLAlchemyError:
            _LOG.exception("owned market card=%s user=%s", card_id, uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(
            {
                "card": {
                    "id": int(card.id),
                    "name": card.name,
                    "set_code": card.set_code,
                    "set_name": card.set_name,
                    "collector_number": card.collector_number,
                    "image_small_url": card.image_small_url,
                    "image_large_url": card.image_large_url,
                    "tcg_rarity": card.tcg_rarity,
                },
                "quote": quote_payload(quote),
                "history": [{"day": p.day.isoformat(), "usd": p.usd_cents / 100.0} for p in hist],
                "position": position,
                "balance_pokedollars": bal,
                "invest_min": INVEST_MIN_PD,
                "invest_max": INVEST_MAX_PD,
            }
        )

    app.router.add_get(r"/api/market/cards/{card_id}", handle_quote)
    if session_secret:
        app.router.add_get("/api/me/market", handle_my_positions)
        app.router.add_get(r"/api/me/market/cards/{card_id}", handle_owned_card_market)
        app.router.add_post("/api/me/market/buy", handle_buy)
        app.router.add_post("/api/me/market/sell", handle_sell)
    _LOG.info("Market API mounted: GET /api/market/cards/{id}, GET/POST /api/me/market")
