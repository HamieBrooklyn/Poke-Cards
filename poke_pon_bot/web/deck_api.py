"""HTTP endpoints for reading and saving the user's combat deck from the website.

Routes:
* ``GET  /api/me/deck``  — current deck slots with full card details.
* ``PUT  /api/me/deck``  — save deck from an ordered list of ``public_id`` strings.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.combat_deck import (
    MAX_DECK,
    get_saved_instance_ids,
    set_deck_from_public_ids,
)
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def _to_int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _max_attack_damage(attacks: Any) -> int:
    if not isinstance(attacks, list):
        return 0
    best = 0
    for atk in attacks:
        if not isinstance(atk, dict):
            continue
        raw = atk.get("damage")
        if raw is None:
            continue
        digits: list[str] = []
        for ch in str(raw):
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            best = max(best, int("".join(digits)))
    return best


def _serialize_slot(
    inst: UserCardInstance, card: Card, rarity: RarityClass | None
) -> dict[str, Any]:
    return {
        "public_id": inst.public_id,
        "instance_id": inst.id,
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


def register_deck_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
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

    async def handle_get_deck(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        try:
            async with session_factory() as db:
                ids = await get_saved_instance_ids(db, session.user_id)
                slots: list[dict[str, Any] | None] = []
                if ids:
                    for iid in ids:
                        inst = await db.get(UserCardInstance, iid)
                        if inst is None or inst.discord_user_id != session.user_id:
                            slots.append(None)
                            continue
                        card = await db.get(Card, inst.card_id)
                        if card is None:
                            slots.append(None)
                            continue
                        rar = await db.get(RarityClass, card.rarity_class_id) if card.rarity_class_id else None
                        slots.append(_serialize_slot(inst, card, rar))
                while len(slots) < MAX_DECK:
                    slots.append(None)
        except SQLAlchemyError:
            _LOG.exception("deck_api GET user=%s", session.user_id)
            return web.json_response({"error": "database error"}, status=500)

        return web.json_response({"max_slots": MAX_DECK, "slots": slots})

    async def handle_put_deck(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "invalid JSON body"}, status=400)

        public_ids = body.get("public_ids")
        if not isinstance(public_ids, list):
            return web.json_response(
                {"error": "body must contain a 'public_ids' array"}, status=400
            )
        public_ids = [str(p).strip() for p in public_ids if str(p).strip()]

        try:
            async with session_factory() as db:
                err = await set_deck_from_public_ids(db, session.user_id, public_ids)
                if err:
                    return web.json_response({"error": err}, status=422)
                await db.commit()

                ids = await get_saved_instance_ids(db, session.user_id)
                slots: list[dict[str, Any] | None] = []
                if ids:
                    for iid in ids:
                        inst = await db.get(UserCardInstance, iid)
                        if inst is None or inst.discord_user_id != session.user_id:
                            slots.append(None)
                            continue
                        card = await db.get(Card, inst.card_id)
                        if card is None:
                            slots.append(None)
                            continue
                        rar = await db.get(RarityClass, card.rarity_class_id) if card.rarity_class_id else None
                        slots.append(_serialize_slot(inst, card, rar))
                while len(slots) < MAX_DECK:
                    slots.append(None)
        except SQLAlchemyError:
            _LOG.exception("deck_api PUT user=%s", session.user_id)
            return web.json_response({"error": "database error"}, status=500)

        return web.json_response({"max_slots": MAX_DECK, "slots": slots})

    app.router.add_get("/api/me/deck", handle_get_deck)
    app.router.add_put("/api/me/deck", handle_put_deck)
