"""Auction JSON API for the public website (OAuth session cookie)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web
from discord import utils as d_utils
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from poke_pon_bot.models.auction import (
    AUCTION_BID_CURRENCY_CRYSTALS,
    AUCTION_STATUS_ACTIVE,
    AuctionBid,
    CardAuction,
)
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.auction_runtime import (
    AuctionSettlement,
    auction_amount_display,
    max_bid_for_currency,
    normalize_auction_bid_currency,
    parse_auction_duration_minutes,
    place_auction_bid,
    settle_due_auctions,
)
from poke_pon_bot.services.auction_search import search_auctions
from poke_pon_bot.services.combat_deck import strip_instances_from_deck
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.instance_public_id import normalize_public_id
from poke_pon_bot.services.trades import MAX_TRADE_CRYSTALS, MAX_TRADE_POKEDOLLARS
from poke_pon_bot.services.wallet import WalletService
from poke_pon_bot.web.sessions import read_session
from poke_pon_bot.web.user_profiles import resolve_user_profiles

_LOG = logging.getLogger(__name__)


def _utc_iso(dt: datetime | None) -> str | None:
    """Serialize a datetime to ISO-8601 with an explicit UTC offset.

    SQLite returns naive datetimes even for ``DateTime(timezone=True)`` columns.
    Appending ``+00:00`` ensures the browser parses them as UTC, not local time.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).isoformat()
    return dt.astimezone(UTC).isoformat()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _serialize_auction_summary(
    auc: CardAuction,
    inst: UserCardInstance,
    card: Card,
    *,
    rarity: RarityClass | None,
    bid_count: int,
    seller: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cur = normalize_auction_bid_currency(auc.bid_currency)
    high = auc.high_bid_pokedollars
    nxt = None
    if high is None:
        min_bid = int(auc.price_pokedollars)
    else:
        min_bid = int(high) + 1
        cap = max_bid_for_currency(cur)
        if min_bid <= cap:
            nxt = min_bid
    seller_id = int(auc.seller_discord_id)
    seller_block = seller or {
        "id": str(seller_id),
        "username": None,
        "global_name": None,
        "avatar_url": None,
    }
    return {
        "id": auc.id,
        "seller_discord_id": str(seller_id),
        "seller": seller_block,
        "guild_id": str(auc.guild_id) if auc.guild_id is not None else None,
        "bid_currency": cur,
        "starting_bid": int(auc.price_pokedollars),
        "high_bid": int(high) if high is not None else None,
        "min_next_bid": nxt,
        "ends_at": _utc_iso(auc.ends_at),
        "created_at": _utc_iso(auc.created_at),
        "bid_count": bid_count,
        "card": {
            "name": card.name,
            "public_id": inst.public_id,
            "set_name": card.set_name,
            "set_code": card.set_code,
            "collector_number": card.collector_number,
            "image_small_url": card.image_small_url,
            "image_large_url": card.image_large_url,
            "tcg_rarity": card.tcg_rarity,
            "rarity_display": rarity.display_name if rarity else None,
        },
    }


def register_auction_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds
    wallet = WalletService()
    crystals = CrystalsService()

    def _require_session(request: web.Request):
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            raise web.HTTPUnauthorized(
                text='{"error":"unauthenticated"}',
                content_type="application/json",
            )
        return sess

    async def handle_balances(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        uid = int(session.user_id)
        try:
            async with session_factory() as db:
                pd = await wallet.get_balance(db, uid)
                cr = await crystals.get_balance(db, uid)
        except SQLAlchemyError:
            _LOG.exception("api balances uid=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(
            {
                "pokedollars": pd,
                "crystals": cr,
            }
        )

    async def handle_list(request: web.Request) -> web.StreamResponse:
        q = (request.query.get("q") or "").strip() or None
        seller_raw = (request.query.get("seller_id") or "").strip()
        seller_id = int(seller_raw) if seller_raw.isdigit() else None
        guild_raw = (request.query.get("guild_id") or "").strip()
        guild_id = int(guild_raw) if guild_raw.isdigit() else None
        sort = (request.query.get("sort") or "popular").strip().lower()
        if sort not in ("popular", "ending", "newest"):
            sort = "popular"
        try:
            page = max(1, int(request.query.get("page", "1")))
        except ValueError:
            page = 1
        try:
            limit = int(request.query.get("limit", "24"))
        except ValueError:
            limit = 24
        offset = (page - 1) * limit

        try:
            await settle_due_auctions(session_factory, wallet, crystals)
        except Exception:
            _LOG.exception("auction settlement during list")

        try:
            async with session_factory() as db:
                rows, total = await search_auctions(
                    db,
                    name_contains=q,
                    rarity_contains=None,
                    pokedex=None,
                    seller_discord_id=seller_id,
                    guild_id=guild_id,
                    page_limit=limit,
                    page_offset=offset,
                    sort=sort,
                    browse_all_if_no_card_filters=True,
                )
                ids = [a.id for a, _, _ in rows]
                counts: dict[int, int] = {}
                if ids:
                    rc = await db.execute(
                        select(AuctionBid.auction_id, func.count(AuctionBid.id))
                        .where(AuctionBid.auction_id.in_(ids))
                        .group_by(AuctionBid.auction_id),
                    )
                    counts = {int(r[0]): int(r[1]) for r in rc.all()}

                seller_ids = {int(auc.seller_discord_id) for auc, _, _ in rows}
                profiles = await resolve_user_profiles(db, bot, seller_ids)

                out = []
                for auc, inst, card in rows:
                    rar = await db.get(RarityClass, card.rarity_class_id)
                    sid = int(auc.seller_discord_id)
                    out.append(
                        _serialize_auction_summary(
                            auc,
                            inst,
                            card,
                            rarity=rar,
                            bid_count=counts.get(auc.id, 0),
                            seller=profiles.get(sid),
                        )
                    )
        except SQLAlchemyError:
            _LOG.exception("auction list api")
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response(
            {
                "auctions": out,
                "total": total,
                "page": page,
                "page_size": limit,
            }
        )

    async def handle_detail(request: web.Request) -> web.StreamResponse:
        try:
            aid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)

        try:
            await settle_due_auctions(session_factory, wallet, crystals)
        except Exception:
            _LOG.exception("auction settlement during detail")

        try:
            async with session_factory() as db:
                auc = await db.get(CardAuction, aid)
                if auc is None or auc.status != AUCTION_STATUS_ACTIVE:
                    return web.json_response({"error": "not_found"}, status=404)
                inst = await db.get(UserCardInstance, auc.instance_id)
                if inst is None:
                    return web.json_response({"error": "not_found"}, status=404)
                card = await db.get(Card, inst.card_id)
                if card is None:
                    return web.json_response({"error": "not_found"}, status=404)
                rar = await db.get(RarityClass, card.rarity_class_id)
                bid_rows = await db.execute(
                    select(AuctionBid)
                    .where(AuctionBid.auction_id == aid)
                    .order_by(AuctionBid.created_at.desc())
                    .limit(80),
                )
                bid_list = list(bid_rows.scalars())
                bidder_ids = {int(b.bidder_discord_id) for b in bid_list}
                profile_ids = bidder_ids | {int(auc.seller_discord_id)}
                profiles = await resolve_user_profiles(db, bot, profile_ids)

                bids = []
                for b in bid_list:
                    bidder_id = int(b.bidder_discord_id)
                    bids.append(
                        {
                            "bidder_discord_id": str(bidder_id),
                            "bidder": profiles.get(
                                bidder_id,
                                {
                                    "id": str(bidder_id),
                                    "username": None,
                                    "global_name": None,
                                    "avatar_url": None,
                                },
                            ),
                            "amount": b.amount,
                            "currency": b.currency,
                            "display": auction_amount_display(b.amount, b.currency),
                            "created_at": _utc_iso(b.created_at),
                        }
                    )
                bid_total = await db.scalar(
                    select(func.count(AuctionBid.id)).where(AuctionBid.auction_id == aid),
                )
                payload = _serialize_auction_summary(
                    auc,
                    inst,
                    card,
                    rarity=rar,
                    bid_count=int(bid_total or 0),
                    seller=profiles.get(int(auc.seller_discord_id)),
                )
                payload["bids"] = bids
        except SQLAlchemyError:
            _LOG.exception("auction detail api id=%s", aid)
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response(payload)

    async def handle_create(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        uid = int(session.user_id)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)

        raw_pid = str(body.get("card_public_id") or "").strip()
        n = normalize_public_id(raw_pid)
        if n is None:
            return web.json_response({"error": "invalid_card_id"}, status=400)

        starting = _to_int(body.get("starting_bid"), 0)
        dur_raw = str(body.get("duration") or body.get("duration_text") or "").strip()
        minutes, err = parse_auction_duration_minutes(dur_raw)
        if err is not None or minutes is None:
            return web.json_response({"error": "invalid_duration", "message": err}, status=400)

        cur = normalize_auction_bid_currency(str(body.get("currency") or body.get("bid_currency") or ""))
        if cur == "":
            return web.json_response({"error": "invalid_currency"}, status=400)

        cap = MAX_TRADE_CRYSTALS if cur == AUCTION_BID_CURRENCY_CRYSTALS else MAX_TRADE_POKEDOLLARS
        if starting < 1 or starting > cap:
            return web.json_response({"error": "invalid_starting_bid"}, status=400)

        guild_body = body.get("guild_id")
        guild_id: int | None = None
        if guild_body is not None and str(guild_body).strip().isdigit():
            guild_id = int(str(guild_body).strip())

        ends_at = d_utils.utcnow() + timedelta(minutes=minutes)

        try:
            async with session_factory() as db:
                row = await db.execute(
                    select(UserCardInstance, Card)
                    .join(Card, UserCardInstance.card_id == Card.id)
                    .where(UserCardInstance.discord_user_id == uid, UserCardInstance.public_id == n)
                    .limit(1),
                )
                first = row.first()
                if first is None:
                    return web.json_response({"error": "card_not_owned"}, status=404)
                inst, _card = first
                dup = await db.scalar(
                    select(CardAuction.id).where(
                        CardAuction.instance_id == inst.id,
                        CardAuction.status == AUCTION_STATUS_ACTIVE,
                    ),
                )
                if dup is not None:
                    return web.json_response({"error": "already_listed"}, status=409)
                await strip_instances_from_deck(db, uid, {inst.id})
                listing = CardAuction(
                    seller_discord_id=uid,
                    guild_id=guild_id,
                    instance_id=inst.id,
                    price_pokedollars=starting,
                    ends_at=ends_at,
                    bid_currency=cur,
                    status="active",
                )
                db.add(listing)
                await db.flush()
                lid = listing.id
                await db.commit()
        except IntegrityError:
            return web.json_response({"error": "already_listed"}, status=409)
        except SQLAlchemyError:
            _LOG.exception("auction create web uid=%s", uid)
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response({"ok": True, "auction_id": lid})

    async def handle_bid(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        uid = int(session.user_id)
        try:
            aid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid_id"}, status=400)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        amount = _to_int(body.get("amount"), 0)

        try:
            await settle_due_auctions(session_factory, wallet, crystals)
        except Exception:
            _LOG.exception("auction settlement during bid")

        try:
            async with session_factory() as db:
                err = await place_auction_bid(
                    db,
                    wallet,
                    crystals,
                    auction_id=aid,
                    bidder_discord_id=uid,
                    amount=amount,
                )
                if err is not None:
                    return web.json_response({"error": "bid_rejected", "message": err}, status=400)
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("auction bid web uid=%s aid=%s", uid, aid)
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response({"ok": True})

    app.router.add_get("/api/me/balances", handle_balances)
    app.router.add_get("/api/auctions", handle_list)
    app.router.add_get(r"/api/auctions/{id:\d+}", handle_detail)
    app.router.add_post("/api/auctions", handle_create)
    app.router.add_post(r"/api/auctions/{id:\d+}/bid", handle_bid)
