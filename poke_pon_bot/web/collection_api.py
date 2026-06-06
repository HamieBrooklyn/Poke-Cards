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
from sqlalchemy import Integer, String, and_, desc, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.collection_sell import (
    collection_sell_base_payout,
    collection_sell_block_reason,
    collection_sell_block_reasons_for_instances,
    collection_sell_needs_confirm,
    quote_collection_sell_payout,
    run_collection_sell,
)
from poke_pon_bot.services.grading import grade_sell_bonus_percent
from poke_pon_bot.services.card_roles import craft_role_for_card, craft_uses_payload
from poke_pon_bot.services.collection_evolution_search import build_evolution_line_sections
from poke_pon_bot.services.collection_search import collection_text_search_clause
from poke_pon_bot.services.collection_visibility import user_instance_not_in_active_auction
from poke_pon_bot.services.evolution import (
    catalog_card_has_evolution_targets_expr,
    catalog_card_lacks_evolution_targets_expr,
    quote_evolution,
    resolve_evolution_targets,
    run_collection_evolution,
)
from poke_pon_bot.services.combat_deck import strip_instances_from_deck
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.grading import (
    GRADE_CRYSTAL_COST,
    build_grade_preview,
    global_copy_index,
    grade_label,
    grading_api_payload,
    load_owned_instance_for_grading,
    remove_grade,
    roll_grade_for_instance,
)
from poke_pon_bot.services.grade_slab import render_graded_slab_png
from poke_pon_bot.services.instance_favorite import toggle_instance_favorite
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


def _primary_dex(card: Card) -> int:
    """First Pokédex number on a card, or a large sentinel when missing."""
    dns = card.dex_numbers
    if isinstance(dns, list) and dns:
        try:
            return int(dns[0])
        except (TypeError, ValueError):
            pass
    return 999_999


def _dex0_int_column():
    dex0 = func.json_extract(Card.dex_numbers, "$[0]")
    return func.cast(func.coalesce(dex0, "999999"), Integer())


def _apply_collection_sort(base, *, sort: str, duplicates_only: bool):
    """When ``duplicates_only``, group copies of the same species (dex #) together."""
    if duplicates_only:
        dex0_int = _dex0_int_column()
        if sort == "rarity":
            return base.order_by(
                dex0_int,
                Card.name,
                desc(RarityClass.sort_order),
                desc(UserCardInstance.obtained_at),
            )
        if sort == "hp":
            hp_int = func.cast(
                func.coalesce(func.nullif(Card.hp, ""), "0"),
                Integer(),
            )
            return base.order_by(
                dex0_int,
                Card.name,
                desc(hp_int),
                desc(UserCardInstance.obtained_at),
            )
        return base.order_by(dex0_int, Card.name, desc(UserCardInstance.obtained_at))

    if sort == "rarity":
        return base.order_by(
            desc(RarityClass.sort_order),
            desc(UserCardInstance.obtained_at),
        )
    if sort == "hp":
        hp_int = func.cast(
            func.coalesce(func.nullif(Card.hp, ""), "0"),
            Integer(),
        )
        return base.order_by(desc(hp_int), desc(UserCardInstance.obtained_at))
    if sort == "damage":
        return base.order_by(desc(UserCardInstance.obtained_at))
    return base.order_by(desc(UserCardInstance.obtained_at))


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
    base_quote = collection_sell_base_payout(card, rarity, inst)
    quote = quote_collection_sell_payout(card, rarity, inst)
    bonus_pct = grade_sell_bonus_percent(inst.grade)
    payload: dict[str, Any] = {
        "quote_pokedollars": quote,
        "needs_confirm": collection_sell_needs_confirm(rarity),
        "blocked_reason": blocked_reason,
        "can_sell": blocked_reason is None,
    }
    if bonus_pct > 0:
        payload["base_quote_pokedollars"] = base_quote
        payload["grade_bonus_percent"] = bonus_pct
    return payload


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
        "auction_obtained_at": _utc_iso(inst.auction_obtained_at),
        "evolution_stages": int(inst.evolution_stages),
        "source": inst.source,
        "is_favorite": bool(inst.is_favorite),
        "craft_role": craft_role_for_card(card),
        "craft_uses": craft_uses_payload(inst, card),
        "sell": sell,
        "card": {
            "name": card.name,
            "set_code": card.set_code,
            "set_name": card.set_name,
            "collector_number": card.collector_number,
            "dex_numbers": card.dex_numbers if isinstance(card.dex_numbers, list) else [],
            "image_small_url": card.image_small_url,
            "image_large_url": card.image_large_url,
            "supertype": card.supertype,
            "tcg_subtypes": card.tcg_subtypes or [],
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


async def _grading_payload(
    db: Any,
    inst: UserCardInstance,
    *,
    full: bool = False,
) -> dict[str, Any]:
    """Website ``grading`` object — full preview on card detail, lighter on list pages."""
    if full:
        preview = await build_grade_preview(db, inst)
        payload = grading_api_payload(preview)
        if preview.graded_at is not None:
            payload["graded_at"] = _utc_iso(preview.graded_at)
    else:
        g = inst.grade
        payload = {
            "grade": g,
            "grade_label": grade_label(g) if g is not None else None,
            "graded_at": _utc_iso(inst.graded_at),
            "crystal_cost": GRADE_CRYSTAL_COST,
            "can_roll": True,
            "can_remove": g is not None,
        }
    if inst.grade is not None and inst.public_id:
        payload["slab_url"] = f"/api/me/cards/{inst.public_id}/slab"
    return payload


async def _serialize_instance_row(
    db: Any,
    inst: UserCardInstance,
    card: Card,
    rarity: RarityClass | None,
    *,
    sell: dict[str, Any],
    grading_full: bool = False,
) -> dict[str, Any]:
    payload = _serialize_instance(inst, card, rarity, sell=sell)
    payload["grading"] = await _grading_payload(db, inst, full=grading_full)
    return payload


async def _evo_target_dict(
    db: Any,
    t: Card,
    *,
    cost: int | None = None,
    include_next: bool = True,
) -> dict[str, Any]:
    """Serialize a single evolution target Card for the frontend."""
    t_rc = await db.get(RarityClass, t.rarity_class_id)
    d: dict[str, Any] = {
        "card_id": int(t.id),
        "name": t.name,
        "image_small_url": t.image_small_url,
        "image_large_url": t.image_large_url,
        "set_code": t.set_code,
        "set_name": t.set_name,
        "collector_number": t.collector_number,
        "supertype": t.supertype,
        "hp": _to_int_or_zero(t.hp),
        "types": t.tcg_types or [],
        "attacks": t.attacks or [],
        "max_damage": _max_attack_damage(t.attacks),
        "tcg_rarity": t.tcg_rarity,
        "rarity_display": t_rc.display_name if t_rc else None,
        "cost_pokedollars": cost,
    }
    if include_next:
        next_cards = await resolve_evolution_targets(db, t)
        next_list: list[dict[str, Any]] = []
        for nt in next_cards:
            next_list.append(await _evo_target_dict(db, nt, include_next=False))
        d["next_targets"] = next_list
    return d


async def _build_evolution_payload(
    db: Any,
    inst: UserCardInstance,
    card: Card,
    rarity: RarityClass | None,
) -> dict[str, Any]:
    """Build the ``evolution`` dict the website expects on card detail."""
    targets = await resolve_evolution_targets(db, card)
    blocked_reason: str | None = None
    target_list: list[dict[str, Any]] = []
    for t in targets:
        t_rc = await db.get(RarityClass, t.rarity_class_id)
        cost: int | None = None
        if rarity is not None and t_rc is not None:
            q = quote_evolution(rarity, inst.evolution_stages, t, t_rc)
            cost = q.cost
        target_list.append(await _evo_target_dict(db, t, cost=cost))
    return {
        "can_evolve": bool(target_list) and blocked_reason is None,
        "targets": target_list,
        "evolution_stages": int(inst.evolution_stages),
        "blocked_reason": blocked_reason,
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
        supertype_filter = (request.query.get("supertype") or "").strip()
        favorited_only = request.query.get("favorited") in ("1", "true")
        evolvable_only = request.query.get("evolvable") in ("1", "true")
        non_evolvable_only = request.query.get("non_evolvable") in ("1", "true")
        duplicates_only = request.query.get("duplicates") in ("1", "true")
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
                needs_card_join = bool(
                    q
                    or supertype_filter
                    or evolvable_only
                    or non_evolvable_only
                    or duplicates_only
                )
                if needs_card_join:
                    count_stmt = count_stmt.join(
                        Card, Card.id == UserCardInstance.card_id
                    )
                if duplicates_only:
                    dex0 = func.json_extract(Card.dex_numbers, "$[0]")
                    dup_dex = (
                        select(dex0)
                        .join(UserCardInstance, UserCardInstance.card_id == Card.id)
                        .where(
                            UserCardInstance.discord_user_id == session.user_id,
                            user_instance_not_in_active_auction(),
                            Card.supertype == "Pokémon",
                            dex0.isnot(None),
                        )
                        .group_by(dex0)
                        .having(func.count(UserCardInstance.id) > 1)
                    )
                    count_stmt = count_stmt.where(
                        Card.supertype == "Pokémon",
                        dex0.in_(dup_dex),
                    )
                if q:
                    search_clause = collection_text_search_clause(q)
                    if search_clause is not None:
                        count_stmt = count_stmt.where(search_clause)
                if supertype_filter:
                    count_stmt = count_stmt.where(Card.supertype == supertype_filter)
                if favorited_only:
                    count_stmt = count_stmt.where(UserCardInstance.is_favorite.is_(True))
                if evolvable_only:
                    count_stmt = count_stmt.where(
                        catalog_card_has_evolution_targets_expr()
                    )
                elif non_evolvable_only:
                    count_stmt = count_stmt.where(
                        catalog_card_lacks_evolution_targets_expr()
                    )
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
                if duplicates_only:
                    dex0 = func.json_extract(Card.dex_numbers, "$[0]")
                    dup_dex = (
                        select(dex0)
                        .join(UserCardInstance, UserCardInstance.card_id == Card.id)
                        .where(
                            UserCardInstance.discord_user_id == session.user_id,
                            user_instance_not_in_active_auction(),
                            Card.supertype == "Pokémon",
                            dex0.isnot(None),
                        )
                        .group_by(dex0)
                        .having(func.count(UserCardInstance.id) > 1)
                    )
                    base = base.where(
                        Card.supertype == "Pokémon",
                        dex0.in_(dup_dex),
                    )
                if q:
                    search_clause = collection_text_search_clause(q)
                    if search_clause is not None:
                        base = base.where(search_clause)
                if supertype_filter:
                    base = base.where(Card.supertype == supertype_filter)
                if favorited_only:
                    base = base.where(UserCardInstance.is_favorite.is_(True))
                if evolvable_only:
                    base = base.where(catalog_card_has_evolution_targets_expr())
                elif non_evolvable_only:
                    base = base.where(catalog_card_lacks_evolution_targets_expr())

                base = _apply_collection_sort(
                    base, sort=sort, duplicates_only=duplicates_only
                )

                if sort == "damage":
                    rows = (await db.execute(base.limit(_DAMAGE_SORT_FETCH_LIMIT))).all()
                    if duplicates_only:
                        ordered = sorted(
                            rows,
                            key=lambda r: (
                                _primary_dex(r[1]),
                                (r[1].name or "").casefold(),
                                -_max_attack_damage(r[1].attacks),
                                -(
                                    r[0].obtained_at.timestamp()
                                    if r[0].obtained_at
                                    else 0.0
                                ),
                            ),
                        )
                    else:
                        ordered = sorted(
                            rows,
                            key=lambda r: (
                                -_max_attack_damage(r[1].attacks),
                                -(
                                    r[0].obtained_at.timestamp()
                                    if r[0].obtained_at
                                    else 0.0
                                ),
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
                items = []
                for inst, card, rar in sliced:
                    items.append(
                        await _serialize_instance_row(
                            db,
                            inst,
                            card,
                            rar,
                            sell=_sell_payload_for_copy(
                                inst, card, rar, block_map.get(inst.id)
                            ),
                        )
                    )
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
                payload = await _serialize_instance_row(
                    db,
                    inst,
                    card,
                    rar,
                    sell=_sell_payload_for_copy(inst, card, rar, blocked),
                    grading_full=True,
                )
                payload["evolution"] = await _build_evolution_payload(
                    db, inst, card, rar
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

    async def handle_bulk_sell_quote(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        raw_ids = body.get("public_ids")
        if not isinstance(raw_ids, list):
            return web.json_response({"error": "public_ids must be a list"}, status=400)

        public_ids: list[str] = []
        for x in raw_ids:
            n = normalize_public_id(str(x))
            if n is None:
                continue
            public_ids.append(n)
        public_ids = public_ids[:200]
        if not public_ids:
            return web.json_response(
                {"items": [], "total_pokedollars": 0, "confirm_required": False}
            )

        try:
            async with session_factory() as db:
                rows = (
                    await db.execute(
                        select(UserCardInstance, Card, RarityClass)
                        .join(Card, Card.id == UserCardInstance.card_id)
                        .join(
                            RarityClass,
                            RarityClass.id == Card.rarity_class_id,
                            isouter=True,
                        )
                        .where(
                            UserCardInstance.discord_user_id == sess.user_id,
                            UserCardInstance.public_id.in_(public_ids),
                            user_instance_not_in_active_auction(),
                        )
                    )
                ).all()

                inst_by_pid: dict[str, tuple[UserCardInstance, Card, RarityClass | None]] = {}
                for inst, card, rar in rows:
                    inst_by_pid[str(inst.public_id)] = (inst, card, rar)

                blocked_map = await collection_sell_block_reasons_for_instances(
                    db,
                    discord_user_id=sess.user_id,
                    instance_ids=[inst.id for inst, _, _ in inst_by_pid.values()],
                )

                out_items: list[dict[str, Any]] = []
                total = 0
                confirm_required = False

                for pid in public_ids:
                    row = inst_by_pid.get(pid)
                    if row is None:
                        out_items.append(
                            {"public_id": pid, "ok": False, "error": "not_found"}
                        )
                        continue
                    inst, card, rar = row
                    if rar is None:
                        out_items.append(
                            {"public_id": pid, "ok": False, "error": "rarity_missing"}
                        )
                        continue
                    blocked = blocked_map.get(int(inst.id))
                    if blocked is not None:
                        out_items.append(
                            {
                                "public_id": pid,
                                "ok": False,
                                "error": "cannot_sell",
                                "reason": blocked,
                            }
                        )
                        continue
                    quote = int(quote_collection_sell_payout(card, rar, inst))
                    needs = bool(collection_sell_needs_confirm(rar))
                    if needs:
                        confirm_required = True
                    total += quote
                    out_items.append(
                        {
                            "public_id": pid,
                            "ok": True,
                            "quote_pokedollars": quote,
                            "confirm_required": needs,
                        }
                    )
        except SQLAlchemyError:
            _LOG.exception("bulk sell quote user=%s", sess.user_id)
            return web.json_response({"error": "database error"}, status=500)

        return web.json_response(
            {
                "items": out_items,
                "total_pokedollars": int(total),
                "confirm_required": bool(confirm_required),
            }
        )

    async def handle_bulk_sell_commit(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        raw_items = body.get("items")
        if not isinstance(raw_items, list):
            return web.json_response({"error": "items must be a list"}, status=400)
        confirm_rare = bool(body.get("confirm_rare"))

        req: list[tuple[str, int]] = []
        for it in raw_items[:200]:
            if not isinstance(it, dict):
                continue
            pid = normalize_public_id(str(it.get("public_id") or ""))
            if pid is None:
                continue
            try:
                q = int(it.get("expected_payout"))
            except (TypeError, ValueError):
                continue
            req.append((pid, q))
        if not req:
            return web.json_response({"error": "no_items"}, status=400)

        try:
            async with session_factory() as db:
                want_pids = [pid for pid, _ in req]
                rows = (
                    await db.execute(
                        select(UserCardInstance, Card, RarityClass)
                        .join(Card, Card.id == UserCardInstance.card_id)
                        .join(
                            RarityClass,
                            RarityClass.id == Card.rarity_class_id,
                            isouter=True,
                        )
                        .where(
                            UserCardInstance.discord_user_id == sess.user_id,
                            UserCardInstance.public_id.in_(want_pids),
                            user_instance_not_in_active_auction(),
                        )
                    )
                ).all()
                inst_by_pid: dict[str, tuple[UserCardInstance, Card, RarityClass | None]] = {}
                for inst, card, rar in rows:
                    inst_by_pid[str(inst.public_id)] = (inst, card, rar)

                blocked_map = await collection_sell_block_reasons_for_instances(
                    db,
                    discord_user_id=sess.user_id,
                    instance_ids=[inst.id for inst, _, _ in inst_by_pid.values()],
                )

                to_delete: list[UserCardInstance] = []
                delete_ids: set[int] = set()
                total = 0
                any_confirm = False

                for pid, expected in req:
                    row = inst_by_pid.get(pid)
                    if row is None:
                        return web.json_response(
                            {"error": "not_found", "public_id": pid}, status=404
                        )
                    inst, card, rar = row
                    if rar is None:
                        return web.json_response(
                            {"error": "rarity_missing", "public_id": pid}, status=400
                        )
                    blocked = blocked_map.get(int(inst.id))
                    if blocked is not None:
                        return web.json_response(
                            {"error": "cannot_sell", "public_id": pid, "reason": blocked},
                            status=400,
                        )
                    quote = int(quote_collection_sell_payout(card, rar, inst))
                    if quote != int(expected):
                        return web.json_response(
                            {
                                "error": "quote_mismatch",
                                "public_id": pid,
                                "quote_pokedollars": quote,
                            },
                            status=409,
                        )
                    if collection_sell_needs_confirm(rar):
                        any_confirm = True
                    total += quote
                    to_delete.append(inst)
                    delete_ids.add(int(inst.id))

                if any_confirm and not confirm_rare:
                    return web.json_response(
                        {
                            "error": "confirm_required",
                            "message": "Some selected cards are high tier — confirm to proceed.",
                        },
                        status=400,
                    )

                await strip_instances_from_deck(db, sess.user_id, delete_ids)
                for inst in to_delete:
                    await db.delete(inst)
                new_bal = await wallet.try_credit(db, sess.user_id, int(total))
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("bulk sell commit user=%s", sess.user_id)
            return web.json_response({"error": "database error"}, status=500)

        return web.json_response(
            {
                "ok": True,
                "payout_pokedollars": int(total),
                "new_balance_pokedollars": int(new_bal),
                "sold_count": len(req),
            }
        )

    async def handle_favorite_card(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        raw = request.match_info.get("public_id", "")
        n = normalize_public_id(raw)
        if n is None:
            return web.json_response({"error": "invalid card id"}, status=400)
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
                new_state = await toggle_instance_favorite(
                    db,
                    discord_user_id=sess.user_id,
                    instance_id=inst.id,
                )
                if new_state is None:
                    return web.json_response({"error": "not found"}, status=404)
                await db.commit()
                blocked = await collection_sell_block_reason(
                    db,
                    discord_user_id=sess.user_id,
                    instance_id=inst.id,
                )
                payload = await _serialize_instance_row(
                    db,
                    inst,
                    card,
                    rar,
                    sell=_sell_payload_for_copy(inst, card, rar, blocked),
                    grading_full=True,
                )
                payload["evolution"] = await _build_evolution_payload(
                    db, inst, card, rar
                )
                return web.json_response(
                    {"ok": True, "is_favorite": new_state, "card": payload}
                )
        except SQLAlchemyError:
            _LOG.exception(
                "favorite_card user=%s public_id=%s", sess.user_id, raw
            )
            return web.json_response({"error": "database error"}, status=500)

    async def handle_evolution_sections(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        q = request.query.get("q", "").strip()
        if not q:
            return web.json_response({"query": "", "sections": []})
        sort = request.query.get("sort", "newest")
        if sort not in _SORT_MODES:
            sort = "newest"
        fav = request.query.get("favorited", "").lower() in ("1", "true", "yes")
        try:
            async with session_factory() as db:
                sections = await build_evolution_line_sections(
                    db,
                    discord_user_id=session.user_id,
                    name_contains=q,
                    favorited_only=fav,
                    sort=sort,
                )
                out: list[dict[str, Any]] = []
                for sec in sections:
                    items: list[dict[str, Any]] = []
                    for inst, card, rar in sec["rows"]:
                        sell_blocked = await collection_sell_block_reason(
                            db,
                            discord_user_id=session.user_id,
                            instance_id=inst.id,
                        )
                        items.append(
                            await _serialize_instance_row(
                                db,
                                inst,
                                card,
                                rar,
                                sell=_sell_payload_for_copy(
                                    inst, card, rar, sell_blocked
                                ),
                            )
                        )
                    out.append(
                        {
                            "key": sec["key"],
                            "label": sec["label"],
                            "total": sec["total"],
                            "items": items,
                        }
                    )
                return web.json_response({"query": q, "sections": out})
        except SQLAlchemyError:
            _LOG.exception(
                "evolution_sections user=%s q=%s", session.user_id, q
            )
            return web.json_response({"error": "database error"}, status=500)

    async def handle_evolve_card(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        raw = request.match_info.get("public_id", "")
        n = normalize_public_id(raw)
        if n is None:
            return web.json_response({"error": "invalid card id"}, status=400)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        target_card_id: int | None = None
        raw_target = body.get("target_card_id")
        if raw_target is not None:
            try:
                target_card_id = int(raw_target)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "target_card_id must be an integer"}, status=400
                )
        try:
            async with session_factory() as db:
                row = (
                    await db.execute(
                        select(UserCardInstance)
                        .where(
                            UserCardInstance.public_id == n,
                            UserCardInstance.discord_user_id == sess.user_id,
                            user_instance_not_in_active_auction(),
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return web.json_response({"error": "not found"}, status=404)
                result = await run_collection_evolution(
                    db,
                    wallet,
                    user_id=sess.user_id,
                    instance_id=row.id,
                    target_card_id=target_card_id,
                )
                if isinstance(result, str):
                    return web.json_response(
                        {"error": "evolve_failed", "reason": result}, status=400
                    )
                new_inst = result.inst
                new_card = result.new_card
                new_rar = await db.get(RarityClass, new_card.rarity_class_id)
                blocked = await collection_sell_block_reason(
                    db,
                    discord_user_id=sess.user_id,
                    instance_id=new_inst.id,
                )
                card_payload = await _serialize_instance_row(
                    db,
                    new_inst,
                    new_card,
                    new_rar,
                    sell=_sell_payload_for_copy(new_inst, new_card, new_rar, blocked),
                    grading_full=True,
                )
                card_payload["evolution"] = await _build_evolution_payload(
                    db, new_inst, new_card, new_rar
                )
                return web.json_response(
                    {
                        "ok": True,
                        "card": card_payload,
                        "before_name": result.before_name,
                        "card_name": new_card.name,
                        "cost_pokedollars": result.cost,
                        "new_balance_pokedollars": result.new_balance,
                    }
                )
        except SQLAlchemyError:
            _LOG.exception(
                "evolve_card user=%s public_id=%s", sess.user_id, raw
            )
            return web.json_response({"error": "database error"}, status=500)

    crystals = CrystalsService()

    async def _card_payload_after_mutation(
        db: Any,
        *,
        inst: UserCardInstance,
        card: Card,
        rar: RarityClass | None,
        user_id: int,
    ) -> dict[str, Any]:
        blocked = await collection_sell_block_reason(
            db,
            discord_user_id=user_id,
            instance_id=inst.id,
        )
        payload = await _serialize_instance_row(
            db,
            inst,
            card,
            rar,
            sell=_sell_payload_for_copy(inst, card, rar, blocked),
            grading_full=True,
        )
        payload["evolution"] = await _build_evolution_payload(db, inst, card, rar)
        return payload

    async def handle_grade_card(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        raw = request.match_info.get("public_id", "")
        n = normalize_public_id(raw)
        if n is None:
            return web.json_response({"error": "invalid card id"}, status=400)
        try:
            async with session_factory() as db:
                loaded = await load_owned_instance_for_grading(
                    db,
                    discord_user_id=sess.user_id,
                    public_id=n,
                )
                if loaded is None:
                    return web.json_response({"error": "not found"}, status=404)
                inst, card = loaded
                outcome = await roll_grade_for_instance(
                    db,
                    crystals,
                    discord_user_id=sess.user_id,
                    instance_id=inst.id,
                )
                if not outcome.ok:
                    return web.json_response(
                        {
                            "ok": False,
                            "error": outcome.error,
                            "message": outcome.error,
                        },
                        status=400,
                    )
                await db.commit()
                await db.refresh(inst)
                rar = await db.get(RarityClass, card.rarity_class_id)
                payload = await _card_payload_after_mutation(
                    db,
                    inst=inst,
                    card=card,
                    rar=rar,
                    user_id=sess.user_id,
                )
                return web.json_response(
                    {
                        "ok": True,
                        "card": payload,
                        "new_crystal_balance": outcome.new_crystal_balance,
                    }
                )
        except SQLAlchemyError:
            _LOG.exception("grade_card user=%s public_id=%s", sess.user_id, raw)
            return web.json_response({"error": "database error"}, status=500)

    async def handle_grade_remove(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        raw = request.match_info.get("public_id", "")
        n = normalize_public_id(raw)
        if n is None:
            return web.json_response({"error": "invalid card id"}, status=400)
        try:
            async with session_factory() as db:
                loaded = await load_owned_instance_for_grading(
                    db,
                    discord_user_id=sess.user_id,
                    public_id=n,
                )
                if loaded is None:
                    return web.json_response({"error": "not found"}, status=404)
                inst, card = loaded
                err = await remove_grade(
                    db,
                    discord_user_id=sess.user_id,
                    instance_id=inst.id,
                )
                if err:
                    return web.json_response(
                        {"ok": False, "error": err, "message": err},
                        status=400,
                    )
                await db.commit()
                await db.refresh(inst)
                rar = await db.get(RarityClass, card.rarity_class_id)
                payload = await _card_payload_after_mutation(
                    db,
                    inst=inst,
                    card=card,
                    rar=rar,
                    user_id=sess.user_id,
                )
                return web.json_response({"ok": True, "card": payload})
        except SQLAlchemyError:
            _LOG.exception("grade_remove user=%s public_id=%s", sess.user_id, raw)
            return web.json_response({"error": "database error"}, status=500)

    async def handle_slab_image(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        raw = request.match_info.get("public_id", "")
        n = normalize_public_id(raw)
        if n is None:
            raise web.HTTPNotFound()
        try:
            async with session_factory() as db:
                loaded = await load_owned_instance_for_grading(
                    db,
                    discord_user_id=sess.user_id,
                    public_id=n,
                )
                if loaded is None:
                    raise web.HTTPNotFound()
                inst, card = loaded
                if inst.grade is None:
                    raise web.HTTPNotFound()
                idx = await global_copy_index(
                    db,
                    card_id=inst.card_id,
                    obtained_at=inst.obtained_at,
                    instance_id=inst.id,
                )
                cert = (inst.public_id or str(inst.id))[:12]
                png = await render_graded_slab_png(
                    card,
                    grade=int(inst.grade),
                    copy_index=idx.copy_index,
                    total_copies=idx.total_copies,
                    cert_suffix=cert,
                )
                if png is None:
                    raise web.HTTPNotFound()
                return web.Response(body=png.read(), content_type="image/png")
        except SQLAlchemyError:
            _LOG.exception("slab_image user=%s public_id=%s", sess.user_id, raw)
            return web.json_response({"error": "database error"}, status=500)

    app.router.add_get("/api/me/collection", handle_collection)
    app.router.add_get("/api/me/collection/evolution-sections", handle_evolution_sections)
    app.router.add_get(r"/api/me/cards/{public_id}", handle_card_detail)
    app.router.add_post(r"/api/me/cards/{public_id}/sell", handle_sell_card)
    app.router.add_post(r"/api/me/cards/bulk-sell/quote", handle_bulk_sell_quote)
    app.router.add_post(r"/api/me/cards/bulk-sell", handle_bulk_sell_commit)
    app.router.add_post(r"/api/me/cards/{public_id}/favorite", handle_favorite_card)
    app.router.add_post(r"/api/me/cards/{public_id}/evolve", handle_evolve_card)
    app.router.add_post(r"/api/me/cards/{public_id}/grade", handle_grade_card)
    app.router.add_post(r"/api/me/cards/{public_id}/grade/remove", handle_grade_remove)
    app.router.add_get(r"/api/me/cards/{public_id}/slab", handle_slab_image)
