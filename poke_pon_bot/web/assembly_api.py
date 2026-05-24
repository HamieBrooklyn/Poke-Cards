"""Card assembly endpoints for the website."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.assembly import (
    list_user_assembly_pieces,
    load_group_info,
    quote_assembly_for_public_ids,
    run_assembly,
    serialize_group_for_api,
)
from poke_pon_bot.services.wallet import WalletService
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def register_assembly_api(app: web.Application, *, bot: Any, settings: Any) -> None:
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

    async def handle_assembly_pieces(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        q = (request.query.get("q") or "").strip()
        anchor = (request.query.get("anchor") or "").strip() or None
        try:
            async with session_factory() as db:
                items = await list_user_assembly_pieces(
                    db,
                    discord_user_id=session.user_id,
                    name_contains=q,
                    anchor_public_id=anchor,
                )
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("assembly_pieces user=%s", session.user_id)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"ok": True, "items": items})

    async def handle_assembly_quote(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_body"}, status=400)
        raw = body.get("public_ids")
        if not isinstance(raw, list):
            return web.json_response({"error": "public_ids_required"}, status=400)
        public_ids = [str(x).strip() for x in raw if str(x).strip()]

        try:
            async with session_factory() as db:
                quote = await quote_assembly_for_public_ids(
                    db,
                    discord_user_id=session.user_id,
                    public_ids=public_ids,
                )
                if isinstance(quote, str):
                    await db.rollback()
                    return web.json_response(
                        {"ok": False, "message": quote.replace("**", "")},
                        status=400,
                    )
                group_info = await load_group_info(db, quote.group_id)
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("assembly_quote user=%s", session.user_id)
            return web.json_response({"error": "database_error"}, status=500)

        payload: dict[str, Any] = {
            "ok": True,
            "cost": quote.cost,
            "display_name": quote.display_name,
            "group_id": quote.group_id,
        }
        if group_info:
            payload["group"] = serialize_group_for_api(group_info)
        return web.json_response(payload)

    async def handle_assembly_assemble(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_body"}, status=400)
        raw = body.get("public_ids")
        if not isinstance(raw, list):
            return web.json_response({"error": "public_ids_required"}, status=400)
        public_ids = [str(x).strip() for x in raw if str(x).strip()]

        wallet = WalletService()
        try:
            async with session_factory() as db:
                outcome = await run_assembly(
                    db,
                    wallet,
                    discord_user_id=session.user_id,
                    public_ids=public_ids,
                )
                if isinstance(outcome, str):
                    await db.rollback()
                    return web.json_response(
                        {"ok": False, "message": outcome.replace("**", "")},
                        status=400,
                    )
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("assembly_assemble user=%s", session.user_id)
            return web.json_response({"error": "database_error"}, status=500)

        inst = outcome.instance
        return web.json_response(
            {
                "ok": True,
                "cost": outcome.cost,
                "new_balance": outcome.new_balance,
                "public_id": inst.public_id,
                "message": (
                    f"Assembled **{outcome.group.display_name}** — "
                    "component pieces were consumed."
                ).replace("**", ""),
                "group": serialize_group_for_api(outcome.group),
                "consumed_pieces": list(outcome.consumed_pieces),
                "card": {
                    "name": outcome.group.result_name,
                    "image_small_url": outcome.group.result_image_small,
                    "image_large_url": outcome.group.result_image_large,
                },
            }
        )

    app.router.add_get("/api/me/assembly/pieces", handle_assembly_pieces)
    app.router.add_post("/api/me/assembly/quote", handle_assembly_quote)
    app.router.add_post("/api/me/assembly/assemble", handle_assembly_assemble)
    _LOG.info(
        "Assembly API mounted at GET /api/me/assembly/pieces, "
        "POST /api/me/assembly/quote, POST /api/me/assembly/assemble"
    )
