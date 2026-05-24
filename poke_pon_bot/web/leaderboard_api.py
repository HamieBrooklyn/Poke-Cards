"""Public leaderboard JSON API for the website."""

from __future__ import annotations

import logging
import math
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.leaderboard import (
    LEADERBOARD_CATEGORIES,
    LEADERBOARD_TITLES,
    fetch_leaderboard_web,
    viewer_rank,
)
from poke_pon_bot.services.wallet import format_pokedollars
from poke_pon_bot.web.sessions import read_session
from poke_pon_bot.web.user_profiles import resolve_user_profiles

_LOG = logging.getLogger(__name__)

_WEB_DEFAULT_LIMIT = 25
_WEB_MAX_LIMIT = 50

_CARD_CATEGORIES = frozenset({"strongest", "tankiest", "rarest", "auction"})


def _serialize_entry(
    category: str,
    rank: int,
    entry: tuple,
    profiles: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    uid = int(entry[0])
    user = profiles.get(uid) or {
        "id": str(uid),
        "username": None,
        "global_name": None,
        "avatar_url": None,
    }
    card_preview_data: dict[str, Any] | None = None

    if category == "strongest":
        _, card_name, dmg, card_preview_data = entry
        payload: dict[str, Any] = {
            "rank": rank,
            "user": user,
            "card_name": card_name,
            "stat": {"kind": "damage", "value": int(dmg), "label": f"{int(dmg)} damage"},
        }
    elif category == "tankiest":
        _, card_name, hp, card_preview_data = entry
        payload = {
            "rank": rank,
            "user": user,
            "card_name": card_name,
            "stat": {"kind": "hp", "value": int(hp), "label": f"{int(hp)} HP"},
        }
    elif category == "rarest":
        _, card_name, rarity_name, _sort_order, card_preview_data = entry
        payload = {
            "rank": rank,
            "user": user,
            "card_name": card_name,
            "stat": {
                "kind": "rarity",
                "value": rarity_name,
                "label": str(rarity_name),
            },
        }
    elif category == "auction":
        _, card_name, price, card_preview_data = entry
        price_int = int(price)
        payload = {
            "rank": rank,
            "user": user,
            "card_name": card_name,
            "stat": {
                "kind": "pokedollars",
                "value": price_int,
                "label": format_pokedollars(price_int),
            },
        }
    else:
        return {"rank": rank, "user": user, "card_name": "", "stat": {"kind": "unknown", "label": ""}}

    if card_preview_data:
        payload["card"] = card_preview_data
        payload["card_preview"] = True
    return payload


def register_leaderboard_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds

    async def handle_leaderboards(request: web.Request) -> web.StreamResponse:
        category = (request.query.get("category") or "strongest").strip().lower()
        if category not in LEADERBOARD_CATEGORIES:
            return web.json_response(
                {
                    "error": "invalid_category",
                    "allowed": sorted(LEADERBOARD_CATEGORIES),
                },
                status=400,
            )

        try:
            page = max(1, int(request.query.get("page", "1")))
        except ValueError:
            page = 1
        try:
            limit = int(request.query.get("limit", str(_WEB_DEFAULT_LIMIT)))
        except ValueError:
            limit = _WEB_DEFAULT_LIMIT
        limit = max(1, min(_WEB_MAX_LIMIT, limit))

        sess = read_session(request, session_secret, max_age=session_ttl)
        viewer_id = int(sess.user_id) if sess is not None else None

        try:
            async with session_factory() as db:
                entries = await fetch_leaderboard_web(db, category)
                total = len(entries)
                total_pages = max(1, math.ceil(total / limit)) if total else 1
                page = min(page, total_pages)
                start = (page - 1) * limit
                end = start + limit
                page_entries = entries[start:end]

                uids = {int(e[0]) for e in page_entries}
                profiles = await resolve_user_profiles(db, bot, uids)

                out_entries = [
                    _serialize_entry(category, start + i + 1, entry, profiles)
                    for i, entry in enumerate(page_entries)
                ]

                rank_for_viewer = (
                    viewer_rank(entries, viewer_id, category) if viewer_id is not None else None
                )
        except ValueError:
            return web.json_response({"error": "invalid_category"}, status=400)
        except SQLAlchemyError:
            _LOG.exception("leaderboard api category=%s", category)
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response(
            {
                "category": category,
                "title": LEADERBOARD_TITLES.get(category, category),
                "scope": "global",
                "card_preview": category in _CARD_CATEGORIES,
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": total_pages,
                "viewer_rank": rank_for_viewer,
                "entries": out_entries,
            }
        )

    app.router.add_get("/api/leaderboards", handle_leaderboards)
