"""HTTP endpoints exposing a signed-in user's bot collection to the website.

The site at ``hamiebrooklyn.github.io`` calls these with ``credentials: 'include'``
so the signed session cookie minted by ``poke_pon_bot.web.oauth`` reaches us.

Shop sell quotes use ``quote_collection_sell_payout`` (same as Discord ``/colv``).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from aiohttp import web
from sqlalchemy import Integer, desc, func, select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.collection_sell import (
    collection_sell_block_reason,
    collection_sell_block_reasons_for_instances,
    collection_sell_needs_confirm,
    quote_collection_sell_payout,
    run_collection_sell,
)
from poke_pon_bot.services.collection_visibility import user_instance_not_in_active_auction
from poke_pon_bot.services.instance_public_id import normalize_public_id
from poke_pon_bot.services.wallet import WalletService
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).isoformat()
    return dt.astimezone(UTC).isoformat()

_DEFAULT_PAGE_SIZE = 60
_MAX_PAGE_SIZE = 120
_DAMAGE_SORT_FETCH_LIMIT = 5000
_SORT_MODES = ("newest", "rarity", "hp", "damage")


def _to_int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _max_attack_damage(attacks: Any) -> int:
    """Best leading-digit ``damage`` field across a card's attacks (0 if none)."""
    if not isinstance(attacks, list):
        return 0
    best = 0
    for atk in attacks:
        if not isinstance(atk, dict):
            continue
        raw = atk.get("damage")
        if raw is None:
            continue
        # Pokémon TCG API damage strings look like "120", "120+", "30x", "" — keep the
        # leading run of digits (so "30x" → 30, "" → 0).
        digits: list[str] = []
        for ch in str(raw):
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            best = max(best, int("".join(digits)))
    return best


def _sell_payload_for_copy(
    inst: UserCardInstance,
    card: Card,
    rarity: RarityClass | None,
    blocked_reason: str | None,
) -> dict[str, Any]:
    """Shop sell terms — same quote and block rules as Discord ``/colv``."""
    if rarity is None:
        return {
            "quote_pokedollars": None,
            "needs_confirm": False,
            "blocked_reason": blocked_reason,
            "can_sell": False,
        }
    quote = quote_collection_sell_payout(card, rarity, inst)
    return {
        "quote_pokedollars": quote,
        "needs_confirm": collection_sell_needs_confirm(rarity),
        "blocked_reason": blocked_reason,
        "can_sell": blocked_reason is None,
    }


def _serialize_instance(
    inst: UserCardInstance,
    card: Card,
    rarity: RarityClass | None,
    *,
    sell: dict[str, Any],
) -> dict[str, Any]:
    return {
        "instance_id": inst.id,
        "public_id": inst.public_id,
        "obtained_at": _utc_iso(inst.obtained_at),
        "evolution_stages": int(inst.evolution_stages),
        "source": inst.source,
        "sell": sell,
        "card": {
            "name": card.name,
            "set_code": card.set_code,
            "set_name": card.set_name,
            "collector_number": card.collector_number,
            "image_small_url": card.image_small_url,
            "image_large_url": card.image_large_url,
            "supertype": card.supertype,
            "hp": _to_int_or_zero(card.hp),
            "types": card.tcg_types or [],
            "attacks": card.attacks or [],
            "max_damage": _max_attack_damage(card.attacks),
            "tcg_rarity": card.tcg_rarity,
            "rarity": {
                "code": rarity.code if rarity else None,
                "display_name": rarity.display_name if rarity else None,
                "sort_order": int(rarity.sort_order) if rarity else 0,
            },
        },
    }


def register_collection_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        _LOG.info("Collection API skipped: WEB_SESSION_SECRET required.")
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds
    wallet = WalletService()

    def _require_session(request: web.Request):
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            raise web.HTTPUnauthorized(
                text='{"error":"unauthenticated"}',
                content_type="application/json",
            )
        return sess

    async def handle_collection(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        q = (request.query.get("q") or "").strip().lower()
        sort = (request.query.get("sort") or "newest").strip().lower()
        if sort not in _SORT_MODES:
            sort = "newest"
        try:
            page = max(1, int(request.query.get("page", "1")))
        except ValueError:
            page = 1
        try:
            page_size = int(request.query.get("page_size", str(_DEFAULT_PAGE_SIZE)))
        except ValueError:
            page_size = _DEFAULT_PAGE_SIZE
        page_size = max(1, min(_MAX_PAGE_SIZE, page_size))

        try:
            async with session_factory() as db:
                count_stmt = select(func.count(UserCardInstance.id)).where(
                    UserCardInstance.discord_user_id == session.user_id,
                    user_instance_not_in_active_auction(),
                )
                if q:
                    count_stmt = count_stmt.join(
                        Card, Card.id == UserCardInstance.card_id
                    ).where(func.lower(Card.name).like(f"%{q}%"))
                total = int((await db.execute(count_stmt)).scalar() or 0)

                base = (
                    select(UserCardInstance, Card, RarityClass)
                    .join(Card, Card.id == UserCardInstance.card_id)
                    .join(
                        RarityClass,
                        RarityClass.id == Card.rarity_class_id,
                        isouter=True,
                    )
                    .where(
                        UserCardInstance.discord_user_id == session.user_id,
                        user_instance_not_in_active_auction(),
                    )
                )
                if q:
                    base = base.where(func.lower(Card.name).like(f"%{q}%"))

                if sort == "rarity":
                    base = base.order_by(
                        desc(RarityClass.sort_order),
                        desc(UserCardInstance.obtained_at),
                    )
                elif sort == "hp":
                    # cards.hp is stored as a String; CAST to integer so "100" > "30"
                    # rather than comparing lexicographically.
                    hp_int = func.cast(
                        func.coalesce(func.nullif(Card.hp, ""), "0"),
                        Integer(),
                    )
                    base = base.order_by(desc(hp_int), desc(UserCardInstance.obtained_at))
                elif sort == "damage":
                    # Damage lives in a JSON list on `cards.attacks`; we sort after fetch.
                    base = base.order_by(desc(UserCardInstance.obtained_at))
                else:
                    base = base.order_by(desc(UserCardInstance.obtained_at))

                if sort == "damage":
                    rows = (await db.execute(base.limit(_DAMAGE_SORT_FETCH_LIMIT))).all()
                    ordered = sorted(
                        rows,
                        key=lambda r: (
                            -_max_attack_damage(r[1].attacks),
                            -(r[0].obtained_at.timestamp() if r[0].obtained_at else 0.0),
                        ),
                    )
                    sliced = ordered[(page - 1) * page_size : page * page_size]
                else:
                    sliced = (
                        await db.execute(
                            base.offset((page - 1) * page_size).limit(page_size)
                        )
                    ).all()

                instance_ids = [inst.id for inst, _, _ in sliced]
                block_map = await collection_sell_block_reasons_for_instances(
                    db,
                    discord_user_id=session.user_id,
                    instance_ids=instance_ids,
                )
                items = [
                    _serialize_instance(
                        inst,
                        card,
                        rar,
                        sell=_sell_payload_for_copy(
                            inst, card, rar, block_map.get(inst.id)
                        ),
                    )
                    for inst, card, rar in sliced
                ]
        except SQLAlchemyError:
            _LOG.exception(
                "collection_api user=%s sort=%s q=%r", session.user_id, sort, q
            )
            return web.json_response({"error": "database error"}, status=500)

        return web.json_response(
            {
                "total": total,
                "page": page,
                "page_size": page_size,
                "sort": sort,
                "query": q,
                "items": items,
            }
        )

    async def handle_card_detail(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        raw_pid = request.match_info.get("public_id", "")
        n = normalize_public_id(raw_pid)
        if n is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            async with session_factory() as db:
                row = (
                    await db.execute(
                        select(UserCardInstance, Card, RarityClass)
                        .join(Card, Card.id == UserCardInstance.card_id)
                        .join(
                            RarityClass,
                            RarityClass.id == Card.rarity_class_id,
                            isouter=True,
                        )
                        .where(
                            UserCardInstance.public_id == n,
                            UserCardInstance.discord_user_id == session.user_id,
                            user_instance_not_in_active_auction(),
                        )
                    )
                ).first()
                if row is None:
                    return web.json_response({"error": "not found"}, status=404)
                inst, card, rar = row
                blocked = await collection_sell_block_reason(
                    db,
                    discord_user_id=session.user_id,
                    instance_id=inst.id,
                )
                payload = _serialize_instance(
                    inst,
                    card,
                    rar,
                    sell=_sell_payload_for_copy(inst, card, rar, blocked),
                )
                return web.json_response(payload)
        except SQLAlchemyError:
            _LOG.exception(
                "card_detail user=%s public_id=%s", session.user_id, raw_pid
            )
            return web.json_response({"error": "database error"}, status=500)

    async def handle_sell_card(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        raw = request.match_info.get("public_id", "")
        n = normalize_public_id(raw)
        if n is None:
            return web.json_response({"error": "invalid card id"}, status=400)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        try:
            expected_int = int(body.get("expected_payout"))
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "expected_payout must be an integer"}, status=400
            )
        confirm_rare = bool(body.get("confirm_rare"))

        try:
            async with session_factory() as db:
                row = (
                    await db.execute(
                        select(UserCardInstance, Card, RarityClass)
                        .join(Card, Card.id == UserCardInstance.card_id)
                        .join(
                            RarityClass,
                            RarityClass.id == Card.rarity_class_id,
                            isouter=True,
                        )
                        .where(
                            UserCardInstance.public_id == n,
                            UserCardInstance.discord_user_id == sess.user_id,
                            user_instance_not_in_active_auction(),
                        )
                    )
                ).first()
                if row is None:
                    return web.json_response({"error": "not found"}, status=404)
                inst, card, rar = row
                if rar is None:
                    return web.json_response(
                        {"error": "rarity data missing — try again after a sync."},
                        status=400,
                    )
                blocked = await collection_sell_block_reason(
                    db,
                    discord_user_id=sess.user_id,
                    instance_id=inst.id,
                )
                if blocked is not None:
                    return web.json_response(
                        {"error": "cannot_sell", "reason": blocked},
                        status=400,
                    )
                quote = quote_collection_sell_payout(card, rar, inst)
                if quote != expected_int:
                    return web.json_response(
                        {
                            "error": "quote_mismatch",
                            "message": "Sell quote changed — refresh and retry.",
                            "quote_pokedollars": quote,
                        },
                        status=409,
                    )
                if collection_sell_needs_confirm(rar) and not confirm_rare:
                    return web.json_response(
                        {
                            "error": "confirm_required",
                            "message": (
                                "This printing is high tier — pass confirm_rare: true "
                                "after acknowledging the sale."
                            ),
                        },
                        status=400,
                    )
                outcome = await run_collection_sell(
                    db,
                    wallet,
                    discord_user_id=sess.user_id,
                    instance_id=inst.id,
                    expected_payout=quote,
                )
        except SQLAlchemyError:
            _LOG.exception(
                "collection sell user=%s public_id=%s", sess.user_id, raw
            )
            return web.json_response({"error": "database error"}, status=500)

        if not outcome.ok:
            return web.json_response(
                {"error": outcome.error or "sell failed"},
                status=400,
            )
        return web.json_response(
            {
                "ok": True,
                "payout_pokedollars": outcome.payout,
                "new_balance_pokedollars": outcome.new_balance,
                "card_name": outcome.card_name,
            }
        )

    app.router.add_get("/api/me/collection", handle_collection)
    app.router.add_get(r"/api/me/cards/{public_id}", handle_card_detail)
    app.router.add_post(r"/api/me/cards/{public_id}/sell", handle_sell_card)
