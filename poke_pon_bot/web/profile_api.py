"""Profile settings API — referrals dashboard and notification preferences."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.guild_referral import GuildReferral
from poke_pon_bot.services.personal_invites import get_or_create_personal_invite
from poke_pon_bot.services.referrals import build_referral_dashboard
from poke_pon_bot.services.web_preferences import (
    get_or_create_web_preferences as _get_prefs,
)
from poke_pon_bot.services.web_preferences import (
    serialize_web_preferences,
    update_web_preferences,
)
from poke_pon_bot.web.sessions import read_session
from poke_pon_bot.web.user_profiles import resolve_user_profiles

_LOG = logging.getLogger(__name__)

_PREF_KEYS = frozenset({"notify_trades", "notify_auctions", "notify_referrals"})


def register_profile_api(app: web.Application, *, bot: Any, settings: Any) -> None:
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

    async def handle_get_settings(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            async with session_factory() as session:
                row = await _get_prefs(session, uid)
                await session.commit()
                payload = serialize_web_preferences(row)
        except SQLAlchemyError:
            _LOG.exception("GET /api/me/settings failed for user %s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"settings": payload})

    async def handle_patch_settings(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_body"}, status=400)

        patch = {k: body[k] for k in _PREF_KEYS if k in body}
        if not patch:
            return web.json_response({"error": "no_valid_fields"}, status=400)

        try:
            async with session_factory() as session:
                payload = await update_web_preferences(session, uid, patch)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("PATCH /api/me/settings failed for user %s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        return web.json_response({"settings": payload})

    async def handle_get_referrals(request: web.Request) -> web.StreamResponse:
        sess = _require_session(request)
        uid = int(sess.user_id)
        invite_guild_id = getattr(settings, "referral_invite_guild_id", None)
        invite_channel_id = getattr(settings, "referral_invite_channel_id", None)
        try:
            async with session_factory() as session:
                rows = await session.execute(
                    select(GuildReferral.invitee_discord_id).where(
                        GuildReferral.inviter_discord_id == uid
                    )
                )
                invitee_ids = [int(x) for x in rows.scalars()]
                profiles = await resolve_user_profiles(session, bot, invitee_ids)
                dashboard = await build_referral_dashboard(
                    session, uid, invitee_profiles=profiles
                )

                personal_invite_url: str | None = None
                if invite_guild_id is not None:
                    invite = await get_or_create_personal_invite(
                        bot,
                        session,
                        discord_user_id=uid,
                        guild_id=int(invite_guild_id),
                        preferred_channel_id=invite_channel_id,
                    )
                    if invite is not None:
                        personal_invite_url = invite.url
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("GET /api/me/referrals failed for user %s", uid)
            return web.json_response({"error": "database_error"}, status=500)
        dashboard["personal_invite_url"] = personal_invite_url
        return web.json_response(dashboard)

    app.router.add_get("/api/me/settings", handle_get_settings)
    app.router.add_patch("/api/me/settings", handle_patch_settings)
    app.router.add_get("/api/me/referrals", handle_get_referrals)
