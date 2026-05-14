"""Discord OAuth2 routes for the public web API (Authorization Code flow).

Flow:

1. Browser hits ``/auth/discord/login?return_to=<frontend_url>``. We mint a random
   ``state`` token, stash it (alongside the ``return_to``) in a short-lived signed
   cookie, then ``302`` to Discord's authorize URL.
2. Discord bounces back to ``/auth/discord/callback?code=...&state=...``. We
   verify ``state`` against the cookie, exchange the code for an access token,
   fetch ``/users/@me``, then mint a long-lived signed session cookie and
   redirect the browser back to ``return_to`` (validated against allowed
   origins so the flow can't be turned into an open redirect).
3. ``/api/me`` reads the session cookie and tells the frontend who is signed in.
4. ``/auth/logout`` clears the session cookie.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from aiohttp import web

from poke_pon_bot.web.sessions import (
    OAUTH_STATE_COOKIE,
    clear_session_cookie,
    decode_oauth_state,
    encode_oauth_state,
    encode_session,
    read_session,
    set_session_cookie,
)

_LOG = logging.getLogger(__name__)

DISCORD_AUTH_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/users/@me"


def _avatar_url(user_id: int, avatar_hash: str | None) -> str | None:
    if not avatar_hash:
        return None
    ext = "gif" if avatar_hash.startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}?size=128"


def _normalize_return_to(
    candidate: str | None,
    fallback: str | None,
    allowed_origins: tuple[str, ...],
) -> str:
    """Only accept return_to values that begin with an allowed origin.

    Prevents the OAuth flow from being abused as an open redirect — a malicious
    link could otherwise bounce users to an attacker-controlled site after they
    sign in.
    """
    value = (candidate or "").strip()
    fb = (fallback or "").strip()
    if value:
        for origin in allowed_origins:
            if value.startswith(origin):
                return value
    return fb or (allowed_origins[0] if allowed_origins else "/")


def _append_fragment_param(url: str, key: str, value: str) -> str:
    """Add a value to the URL fragment without sending it to the web server.

    The session is already a signed, expiring token, but putting it in the
    fragment keeps it out of normal HTTP logs and out of the `return_to` query.
    The static frontend reads it, stores it in localStorage, then strips it from
    the visible address bar.
    """
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.fragment, keep_blank_values=True))
    params[key] = value
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, urlencode(params))
    )


def register_oauth_routes(
    app: web.Application,
    *,
    settings: Any,
    secure_cookies: bool,
    bot: Any = None,
) -> None:
    client_id = settings.discord_oauth_client_id
    client_secret = settings.discord_oauth_client_secret
    session_secret = settings.web_session_secret
    if not client_id or not client_secret or not session_secret:
        _LOG.info(
            "Discord OAuth routes skipped: DISCORD_OAUTH_CLIENT_ID/SECRET and "
            "WEB_SESSION_SECRET are all required."
        )
        return

    public_url = (settings.web_public_url or "").rstrip("/")
    if not public_url:
        _LOG.warning(
            "WEB_PUBLIC_URL not set — OAuth callback URL will be derived from each inbound "
            "request, which only works for purely local testing (Discord rejects tunneled IPs)."
        )

    redirect_uri = f"{public_url}/auth/discord/callback" if public_url else None

    async def handle_login(request: web.Request) -> web.StreamResponse:
        effective_redirect = redirect_uri or str(request.url.with_path("/auth/discord/callback"))
        return_to = request.query.get("return_to")
        state_token = secrets.token_urlsafe(24)
        state_blob = encode_oauth_state(session_secret, return_to=return_to)

        params = {
            "client_id": str(client_id),
            "response_type": "code",
            "redirect_uri": effective_redirect,
            "scope": "identify",
            "state": state_token,
            "prompt": "consent",
        }
        resp = web.HTTPFound(f"{DISCORD_AUTH_URL}?{urlencode(params)}")
        # Pack (state, signed return_to blob) together so the callback can verify both.
        resp.set_cookie(
            OAUTH_STATE_COOKIE,
            f"{state_token}.{state_blob}",
            max_age=600,
            httponly=True,
            secure=secure_cookies,
            samesite="None" if secure_cookies else "Lax",
            path="/",
        )
        return resp

    async def handle_callback(request: web.Request) -> web.StreamResponse:
        effective_redirect = redirect_uri or str(request.url.with_path("/auth/discord/callback"))
        err = request.query.get("error")
        if err:
            return web.Response(status=400, text=f"OAuth error: {err}")

        code = request.query.get("code")
        state = request.query.get("state")
        cookie = request.cookies.get(OAUTH_STATE_COOKIE)
        if not code or not state or not cookie or "." not in cookie:
            return web.Response(status=400, text="Missing OAuth code/state.")
        cookie_token, _, cookie_payload = cookie.partition(".")
        if cookie_token != state:
            return web.Response(status=400, text="OAuth state mismatch.")
        state_data = decode_oauth_state(session_secret, cookie_payload)
        if state_data is None:
            return web.Response(status=400, text="OAuth state expired.")

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                tok = await client.post(
                    DISCORD_TOKEN_URL,
                    data={
                        "client_id": str(client_id),
                        "client_secret": client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": effective_redirect,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                tok.raise_for_status()
                token_data = tok.json()
                access = token_data.get("access_token")
                if not access:
                    return web.Response(status=400, text="Discord did not return an access token.")
                me = await client.get(
                    DISCORD_ME_URL, headers={"Authorization": f"Bearer {access}"}
                )
                me.raise_for_status()
                user = me.json()
            except httpx.HTTPError as exc:
                _LOG.warning("Discord OAuth exchange failed: %s", exc)
                return web.Response(status=502, text="Discord OAuth exchange failed.")

        try:
            user_id = int(user["id"])
        except (KeyError, TypeError, ValueError):
            return web.Response(status=502, text="Discord returned an unexpected payload.")
        username = str(user.get("username") or "")
        global_name = user.get("global_name")
        avatar = _avatar_url(user_id, user.get("avatar"))

        try:
            from poke_pon_bot.models.known_user import KnownUser

            async with bot.async_session_factory() as db:  # type: ignore[union-attr]
                ku = await db.get(KnownUser, user_id)
                if ku is None:
                    ku = KnownUser(discord_id=user_id, username=username, global_name=global_name, avatar_url=avatar)
                    db.add(ku)
                else:
                    ku.username = username
                    ku.global_name = global_name
                    ku.avatar_url = avatar
                    from datetime import UTC, datetime
                    ku.last_seen_at = datetime.now(UTC)
                await db.commit()
        except Exception:
            _LOG.debug("Failed to upsert KnownUser for %s", user_id)

        session_value = encode_session(
            session_secret,
            user_id=user_id,
            username=username,
            global_name=global_name,
            avatar_url=avatar,
        )

        return_to = _normalize_return_to(
            state_data.get("r"),
            settings.web_frontend_url,
            settings.web_allowed_origins,
        )
        resp = web.HTTPFound(_append_fragment_param(return_to, "session", session_value))
        set_session_cookie(
            resp,
            value=session_value,
            ttl_seconds=settings.web_session_ttl_seconds,
            secure=secure_cookies,
        )
        resp.del_cookie(OAUTH_STATE_COOKIE, path="/")
        return resp

    async def handle_me(request: web.Request) -> web.StreamResponse:
        session = read_session(
            request, session_secret, max_age=settings.web_session_ttl_seconds
        )
        if session is None:
            return web.json_response({"authenticated": False})
        return web.json_response(
            {
                "authenticated": True,
                "user": {
                    "id": str(session.user_id),
                    "username": session.username,
                    "global_name": session.global_name,
                    "avatar_url": session.avatar_url,
                },
            }
        )

    async def handle_logout(_request: web.Request) -> web.StreamResponse:
        resp = web.json_response({"ok": True})
        clear_session_cookie(resp, secure=secure_cookies)
        return resp

    app.router.add_get("/auth/discord/login", handle_login)
    app.router.add_get("/auth/discord/callback", handle_callback)
    app.router.add_get("/api/me", handle_me)
    app.router.add_post("/auth/logout", handle_logout)
