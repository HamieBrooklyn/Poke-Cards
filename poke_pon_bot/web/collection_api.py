"""HTTP endpoints exposing a signed-in user's bot collection to the website.

The site at ``hamiebrooklyn.github.io`` calls these with ``credentials: 'include'``
so the signed session cookie minted by ``poke_pon_bot.web.oauth`` reaches us.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from sqlalchemy import Integer, desc, func, select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)

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


def _serialize_instance(
    inst: UserCardInstance, card: Card, rarity: RarityClass | None
) -> dict[str, Any]:
    return {
        "public_id": inst.public_id,
        "obtained_at": inst.obtained_at.isoformat() if inst.obtained_at else None,
        "evolution_stages": int(inst.evolution_stages),
        "source": inst.source,
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
                    UserCardInstance.discord_user_id == session.user_id
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
                    .where(UserCardInstance.discord_user_id == session.user_id)
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

                items = [
                    _serialize_instance(inst, card, rar) for inst, card, rar in sliced
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
        public_id = request.match_info.get("public_id", "")
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
                            UserCardInstance.public_id == public_id,
                            UserCardInstance.discord_user_id == session.user_id,
                        )
                    )
                ).first()
                if row is None:
                    return web.json_response({"error": "not found"}, status=404)
                inst, card, rar = row
                return web.json_response(_serialize_instance(inst, card, rar))
        except SQLAlchemyError:
            _LOG.exception(
                "card_detail user=%s public_id=%s", session.user_id, public_id
            )
            return web.json_response({"error": "database error"}, status=500)

    app.router.add_get("/api/me/collection", handle_collection)
    app.router.add_get(r"/api/me/cards/{public_id}", handle_card_detail)
