"""Global TCG catalog browse for the website Pokédex (not user inventory)."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.card_images import card_image_urls
from poke_pon_bot.services.catalog_search import browse_catalog, catalog_facets
from poke_pon_bot.services.excluded_sets import excluded_set_clause
from poke_pon_bot.web.collection_api import _max_attack_damage, _to_int_or_zero, _utc_iso
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 60
_MAX_PAGE_SIZE = 120


def _optional_session(request: web.Request, settings: Any) -> int | None:
    secret = getattr(settings, "web_session_secret", None)
    if not secret:
        return None
    ttl = int(getattr(settings, "web_session_ttl_seconds", 0) or 0) or 60 * 60 * 24 * 30
    sess = read_session(request, secret, max_age=ttl)
    return sess.user_id if sess else None


def _parse_int_query(request: web.Request, key: str) -> int | None:
    raw = (request.rel_url.query.get(key) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_card_ids_query(request: web.Request) -> list[int] | None:
    raw = (request.rel_url.query.get("card_ids") or "").strip()
    if not raw:
        return None
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out or None


def _serialize_catalog_summary(
    card: Card,
    rarity: RarityClass | None,
    *,
    owned_count: int = 0,
    web_public_url: str | None = None,
) -> dict[str, Any]:
    small_url, large_url = card_image_urls(card, web_public_url=web_public_url)
    return {
        "id": int(card.id),
        "tcg_card_id": card.tcg_card_id,
        "name": card.name,
        "set_code": card.set_code,
        "set_name": card.set_name,
        "collector_number": card.collector_number,
        "supertype": card.supertype,
        "tcg_subtypes": getattr(card, "tcg_subtypes", None) or [],
        "dex_numbers": card.dex_numbers or [],
        "image_small_url": small_url,
        "image_large_url": large_url,
        "hp": _to_int_or_zero(card.hp),
        "max_damage": _max_attack_damage(card.attacks),
        "tcg_rarity": card.tcg_rarity,
        "rarity": {
            "code": rarity.code if rarity else None,
            "display_name": rarity.display_name if rarity else None,
            "sort_order": int(rarity.sort_order) if rarity else 0,
        },
        "owned_count": int(owned_count),
    }


def _serialize_catalog_detail(
    card: Card,
    rarity: RarityClass | None,
    *,
    owned_copies: list[dict[str, Any]],
    web_public_url: str | None = None,
) -> dict[str, Any]:
    payload = _serialize_catalog_summary(
        card, rarity, owned_count=len(owned_copies), web_public_url=web_public_url
    )
    payload.update(
        {
            "types": card.tcg_types or [],
            "attacks": card.attacks or [],
            "evolves_from": card.evolves_from,
            "evolves_to_card_id": card.evolves_to_card_id,
            "owned_copies": owned_copies,
        }
    )
    return payload


async def _owned_counts(
    session: Any,
    *,
    discord_user_id: int,
    card_ids: list[int],
) -> dict[int, int]:
    if not card_ids:
        return {}
    rows = (
        await session.execute(
            select(UserCardInstance.card_id, func.count())
            .where(
                UserCardInstance.discord_user_id == discord_user_id,
                UserCardInstance.card_id.in_(card_ids),
            )
            .group_by(UserCardInstance.card_id)
        )
    ).all()
    return {int(cid): int(cnt) for cid, cnt in rows}


async def _owned_copies_for_card(
    session: Any,
    *,
    discord_user_id: int,
    card_id: int,
    limit: int = 40,
) -> list[dict[str, Any]]:
    from poke_pon_bot.services.grading import grade_label

    rows = (
        await session.execute(
            select(UserCardInstance)
            .where(
                UserCardInstance.discord_user_id == discord_user_id,
                UserCardInstance.card_id == card_id,
            )
            .order_by(UserCardInstance.obtained_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for inst in rows:
        entry: dict[str, Any] = {
            "public_id": inst.public_id,
            "obtained_at": _utc_iso(inst.obtained_at),
            "is_favorite": bool(inst.is_favorite),
            "grade": int(inst.grade) if inst.grade is not None else None,
        }
        if inst.grade is not None:
            entry["grade_label"] = grade_label(int(inst.grade))
        out.append(entry)
    return out


def register_catalog_public_api(app: web.Application, *, bot: Any) -> None:
    """Public read-only global catalog routes (optional session for ownership)."""
    session_factory = bot.async_session_factory
    settings = bot.settings

    async def handle_facets(_request: web.Request) -> web.StreamResponse:
        try:
            async with session_factory() as db:
                data = await catalog_facets(db)
        except SQLAlchemyError:
            _LOG.exception("catalog_api facets")
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response(data)

    async def handle_list(request: web.Request) -> web.StreamResponse:
        q = (request.rel_url.query.get("q") or "").strip() or None
        set_code = (request.rel_url.query.get("set_code") or "").strip() or None
        supertype = (request.rel_url.query.get("supertype") or "").strip() or None
        rarity_tier = (request.rel_url.query.get("rarity_tier") or "").strip() or None
        sort = (request.rel_url.query.get("sort") or "name").strip().lower()
        pokedex = _parse_int_query(request, "pokedex")
        card_ids = _parse_card_ids_query(request)
        try:
            page = max(1, int(request.rel_url.query.get("page") or "1"))
        except ValueError:
            page = 1
        try:
            page_size = max(1, min(int(request.rel_url.query.get("page_size") or _DEFAULT_PAGE_SIZE), _MAX_PAGE_SIZE))
        except ValueError:
            page_size = _DEFAULT_PAGE_SIZE

        user_id = _optional_session(request, settings)
        try:
            async with session_factory() as db:
                rows, total = await browse_catalog(
                    db,
                    q=q,
                    set_code=set_code,
                    supertype=supertype,
                    rarity_tier=rarity_tier,
                    pokedex=pokedex,
                    sort=sort,
                    page=page,
                    page_size=page_size,
                    card_ids=card_ids,
                )
                owned_map: dict[int, int] = {}
                if user_id is not None and rows:
                    card_ids = [int(card.id) for card, _ in rows]
                    owned_map = await _owned_counts(db, discord_user_id=user_id, card_ids=card_ids)
        except SQLAlchemyError:
            _LOG.exception("catalog_api list")
            return web.json_response({"error": "database_error"}, status=500)

        public_base = getattr(settings, "web_public_url", None)
        items = [
            _serialize_catalog_summary(
                card,
                rarity,
                owned_count=owned_map.get(int(card.id), 0),
                web_public_url=public_base,
            )
            for card, rarity in rows
        ]
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        return web.json_response(
            {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "authenticated": user_id is not None,
            }
        )

    async def handle_card_detail(request: web.Request) -> web.StreamResponse:
        raw_id = request.match_info.get("card_id", "").strip()
        try:
            card_id = int(raw_id)
        except ValueError:
            return web.json_response({"error": "invalid_card_id"}, status=400)

        user_id = _optional_session(request, settings)
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
                owned: list[dict[str, Any]] = []
                if user_id is not None:
                    owned = await _owned_copies_for_card(
                        db, discord_user_id=user_id, card_id=int(card.id)
                    )
        except SQLAlchemyError:
            _LOG.exception("catalog_api card detail card_id=%s", card_id)
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response(
            _serialize_catalog_detail(
                card,
                rarity,
                owned_copies=owned,
                web_public_url=getattr(settings, "web_public_url", None),
            )
        )

    app.router.add_get("/api/catalog/facets", handle_facets)
    app.router.add_get("/api/catalog", handle_list)
    app.router.add_get(r"/api/catalog/cards/{card_id}", handle_card_detail)
    _LOG.info(
        "Catalog public API mounted: GET /api/catalog, /api/catalog/facets, "
        "/api/catalog/cards/{card_id}"
    )
