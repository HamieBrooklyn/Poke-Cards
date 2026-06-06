"""Public leaderboard JSON API for the website."""

from __future__ import annotations

import logging
import math
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.crystal_sinks import load_equipped_leaderboard_frames
from poke_pon_bot.services.guild_milestones import (
    GUILD_STAT_CATEGORIES,
    resolve_guild_member_ids,
)
from poke_pon_bot.services.leaderboard import (
    LEADERBOARD_CATEGORIES,
    LEADERBOARD_TITLES,
    SERVER_LEADERBOARD_CATEGORIES,
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
    frames: dict[int, str | None] | None = None,
) -> dict[str, Any]:
    uid = int(entry[0])
    user = dict(
        profiles.get(uid)
        or {
            "id": str(uid),
            "username": None,
            "global_name": None,
            "avatar_url": None,
        }
    )
    frame = (frames or {}).get(uid)
    if frame:
        user["leaderboard_frame"] = frame
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
    elif category == "graded":
        _, card_name, grade_val, grade_lbl, card_preview_data = entry
        payload = {
            "rank": rank,
            "user": user,
            "card_name": card_name,
            "stat": {
                "kind": "grade",
                "value": int(grade_val),
                "label": f"{int(grade_val)} — {grade_lbl}",
            },
        }
    elif category == "packs":
        _, count = entry
        payload = {
            "rank": rank,
            "user": user,
            "card_name": "",
            "stat": {
                "kind": "count",
                "value": int(count),
                "label": f"{int(count):,} packs",
            },
        }
    elif category == "traders":
        _, count = entry
        payload = {
            "rank": rank,
            "user": user,
            "card_name": "",
            "stat": {
                "kind": "count",
                "value": int(count),
                "label": f"{int(count):,} trades",
            },
        }
    elif category == "collectors":
        _, count = entry
        payload = {
            "rank": rank,
            "user": user,
            "card_name": "",
            "stat": {
                "kind": "count",
                "value": int(count),
                "label": f"{int(count):,} unique cards",
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
        scope = (request.query.get("scope") or "global").strip().lower()
        guild_raw = (request.query.get("guild_id") or "").strip()
        guild_id = int(guild_raw) if guild_raw.isdigit() else None

        if scope not in ("global", "server"):
            return web.json_response({"error": "invalid_scope"}, status=400)
        if category in GUILD_STAT_CATEGORIES and scope != "server":
            return web.json_response(
                {"error": "server_scope_required", "category": category},
                status=400,
            )
        if scope == "server":
            if guild_id is None:
                return web.json_response({"error": "guild_id_required"}, status=400)
            if category not in SERVER_LEADERBOARD_CATEGORIES:
                return web.json_response(
                    {
                        "error": "invalid_category",
                        "allowed": sorted(SERVER_LEADERBOARD_CATEGORIES),
                    },
                    status=400,
                )
        elif category not in LEADERBOARD_CATEGORIES:
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

        member_ids: set[int] | None = None
        members_unavailable = False
        if scope == "server" and guild_id is not None:
            guild = bot.get_guild(int(guild_id))
            if guild is None:
                return web.json_response(
                    {
                        "error": "guild_not_available",
                        "message": "The bot is not in that server right now.",
                    },
                    status=404,
                )
            async with session_factory() as db:
                member_ids = await resolve_guild_member_ids(bot, db, int(guild_id))
            if not member_ids:
                members_unavailable = True

        try:
            async with session_factory() as db:
                entries = await fetch_leaderboard_web(
                    db,
                    category,
                    member_ids=member_ids,
                    guild_id=guild_id if scope == "server" else None,
                )
                total = len(entries)
                total_pages = max(1, math.ceil(total / limit)) if total else 1
                page = min(page, total_pages)
                start = (page - 1) * limit
                end = start + limit
                page_entries = entries[start:end]

                uids = {int(e[0]) for e in page_entries}
                profiles = await resolve_user_profiles(db, bot, uids)
                frames = await load_equipped_leaderboard_frames(db, uids)

                out_entries = [
                    _serialize_entry(category, start + i + 1, entry, profiles, frames)
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

        guild_name = None
        if scope == "server" and guild_id is not None:
            g = bot.get_guild(guild_id)
            guild_name = g.name if g is not None else f"Server {guild_id}"

        return web.json_response(
            {
                "category": category,
                "title": LEADERBOARD_TITLES.get(category, category),
                "scope": scope,
                "guild_id": str(guild_id) if guild_id is not None else None,
                "guild_name": guild_name,
                "card_preview": category in _CARD_CATEGORIES,
                "members_unavailable": members_unavailable,
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": total_pages,
                "viewer_rank": rank_for_viewer,
                "entries": out_entries,
            }
        )

    app.router.add_get("/api/leaderboards", handle_leaderboards)
