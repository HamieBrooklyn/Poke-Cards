"""HTTP route for Top.gg ``vote.create`` webhooks.

Originally this module owned its own aiohttp server; it now exposes
``register_topgg_routes`` so the bot's single web server (see
``poke_pon_bot.web.server``) can host the webhook alongside OAuth + collection
endpoints on one port.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.topgg_vote import parse_topgg_iso8601
from poke_pon_bot.services.topgg_webhook_verify import verify_topgg_webhook_signature
from poke_pon_bot.services.wallet import WalletService, format_pokedollars

_LOG = logging.getLogger(__name__)


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def register_topgg_routes(
    app: web.Application,
    *,
    bot: Any,
    settings: Any,
) -> None:
    secret = (settings.topgg_webhook_secret or "").strip()
    if not secret:
        return

    path = settings.topgg_webhook_path
    if not path.startswith("/"):
        path = "/" + path

    wallet = WalletService()

    async def handle_post(request: web.Request) -> web.Response:
        raw = await request.read()
        sig = request.headers.get("x-topgg-signature") or request.headers.get(
            "X-Topgg-Signature"
        )
        if not verify_topgg_webhook_signature(
            raw_body=raw,
            signature_header=sig,
            webhook_secret=secret,
            max_clock_skew_seconds=settings.topgg_webhook_max_clock_skew_seconds,
        ):
            _LOG.warning("Top.gg webhook rejected: bad signature or stale timestamp")
            return _json_error("invalid signature", status=401)

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_error("invalid json", status=400)

        event_type = payload.get("type")
        data = payload.get("data")
        if not isinstance(data, dict):
            return _json_error("invalid payload", status=400)

        if event_type == "webhook.test":
            _LOG.info("Top.gg webhook.test OK")
            return web.Response(status=200, text="ok")

        if event_type != "vote.create":
            _LOG.info("Ignoring Top.gg event type %r", event_type)
            return web.Response(status=200, text="ignored")

        proj = data.get("project")
        if isinstance(proj, dict) and proj.get("type") != "bot":
            return web.Response(status=200, text="not a bot vote")

        vote_id = data.get("id")
        user = data.get("user")
        if not isinstance(vote_id, str) or not vote_id.strip():
            return _json_error("missing vote id", status=400)
        if not isinstance(user, dict):
            return _json_error("missing user", status=400)
        platform_id = user.get("platform_id")
        try:
            discord_uid = int(platform_id)
        except (TypeError, ValueError):
            return _json_error("bad platform_id", status=400)

        if settings.topgg_webhook_expected_platform_id is not None:
            exp = str(settings.topgg_webhook_expected_platform_id)
            proj_pid = proj.get("platform_id") if isinstance(proj, dict) else None
            got = str(proj_pid) if proj_pid is not None else ""
            if got and got != exp:
                _LOG.warning(
                    "Top.gg webhook platform_id mismatch: got %r expected %r", got, exp
                )
                return _json_error("project mismatch", status=403)

        created_raw = data.get("created_at")
        expires_raw = data.get("expires_at")
        if not isinstance(created_raw, str) or not isinstance(expires_raw, str):
            return _json_error("missing vote times", status=400)
        try:
            created_at = parse_topgg_iso8601(created_raw)
            expires_at = parse_topgg_iso8601(expires_raw)
        except ValueError:
            return _json_error("invalid timestamps", status=400)

        try:
            async with bot.async_session_factory() as session:
                result = await wallet.apply_topgg_webhook_vote(
                    session,
                    vote_id=vote_id.strip(),
                    discord_user_id=discord_uid,
                    vote_created_at=created_at,
                    vote_expires_at=expires_at,
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception(
                "Top.gg webhook DB error vote_id=%s user=%s", vote_id, discord_uid
            )
            return web.Response(status=500, text="db error")

        if result.kind == "paid":
            _LOG.info(
                "Top.gg vote reward paid user=%s vote=%s amount=%s crystals=%s",
                discord_uid,
                vote_id,
                format_pokedollars(result.amount),
                result.crystals_credited,
            )
        elif result.kind == "duplicate":
            _LOG.debug("Top.gg webhook duplicate vote_id=%s", vote_id)
        elif result.kind == "absorbed":
            _LOG.info(
                "Top.gg webhook absorbed (already paid via /vote) vote=%s user=%s",
                vote_id,
                discord_uid,
            )
        else:
            _LOG.info(
                "Top.gg webhook stale vote_id=%s user=%s", vote_id, discord_uid
            )

        return web.Response(status=200, text="ok")

    app.router.add_post(path, handle_post)
    _LOG.info(
        "Top.gg vote webhook route registered at %s (paste the proxied HTTPS URL "
        "into your Top.gg dashboard).",
        path,
    )
