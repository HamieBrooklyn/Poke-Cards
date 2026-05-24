"""Crafting endpoints for the website."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.card_roles import CRAFT_ITEM_COUNT, CRAFT_TRAINER_MAX_USES
from poke_pon_bot.services.crafting import (
    open_crafted_pack_for_user,
    run_craft,
    serialize_craft_pack,
)
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def register_craft_api(app: web.Application, *, bot: Any, settings: Any) -> None:
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

    async def handle_craft(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_body"}, status=400)

        raw_items = body.get("item_public_ids")
        trainer_id = (body.get("trainer_public_id") or "").strip()
        if not isinstance(raw_items, list):
            return web.json_response({"error": "item_public_ids_required"}, status=400)
        item_ids = [str(x).strip() for x in raw_items if str(x).strip()]

        try:
            async with session_factory() as db:
                outcome = await run_craft(
                    db,
                    discord_user_id=session.user_id,
                    item_public_ids=item_ids,
                    trainer_public_id=trainer_id,
                )
                if isinstance(outcome, str):
                    await db.rollback()
                    return web.json_response(
                        {"ok": False, "message": outcome.replace("**", "")},
                        status=400,
                    )
                await db.commit()
                _LOG.info(
                    "craft_api user=%s pack_id=%s series=%s",
                    session.user_id,
                    outcome.pack.public_id,
                    outcome.series.code,
                )
        except SQLAlchemyError:
            _LOG.exception("craft_api user=%s", session.user_id)
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response(
            {
                "ok": True,
                "item_count": CRAFT_ITEM_COUNT,
                "trainer_max_uses": CRAFT_TRAINER_MAX_USES,
                "pack": serialize_craft_pack(outcome.pack, outcome.series),
                "trainer_name": outcome.trainer_name,
                "trainer_uses_remaining": outcome.trainer_uses_remaining,
                "pack_tier_rarity": outcome.pack_tier_rarity,
            }
        )

    async def handle_craft_open_pack(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        pack_id = (body.get("pack_public_id") or "").strip() if isinstance(body, dict) else ""
        if not pack_id:
            return web.json_response({"error": "pack_public_id_required"}, status=400)

        try:
            async with session_factory() as db:
                outcome = await open_crafted_pack_for_user(
                    db,
                    discord_user_id=session.user_id,
                    pack_public_id=pack_id,
                )
                if isinstance(outcome, str):
                    await db.rollback()
                    return web.json_response(
                        {"ok": False, "message": outcome},
                        status=400,
                    )
                await db.commit()
                pack, series = outcome
        except SQLAlchemyError:
            _LOG.exception("craft_open user=%s", session.user_id)
            return web.json_response({"error": "database_error"}, status=500)

        return web.json_response(
            {
                "ok": True,
                "pack": serialize_craft_pack(pack, series),
                "message": "Pack opened — check your collection for new cards.",
            }
        )

    app.router.add_post("/api/me/craft", handle_craft)
    app.router.add_post("/api/me/craft/open-pack", handle_craft_open_pack)
    _LOG.info("Craft API mounted at POST /api/me/craft")
