"""Signed-cookie sessions for the public web API.

Uses ``itsdangerous.URLSafeTimedSerializer`` so the bot doesn't need a server-side
session store. The cookie payload is a small JSON blob with the Discord user id
and cached profile fields; ``loads(..., max_age=...)`` rejects expired or
tampered cookies in one step. Cross-origin browser requests from GitHub Pages
need ``SameSite=None`` + ``Secure``; we pick those flags based on whether the
public URL uses HTTPS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "pokepon_session"
OAUTH_STATE_COOKIE = "pokepon_oauth_state"


@dataclass(frozen=True)
class SessionUser:
    user_id: int
    username: str
    global_name: str | None
    avatar_url: str | None


def _serializer(secret: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=salt)


def encode_session(
    secret: str,
    *,
    user_id: int,
    username: str,
    global_name: str | None,
    avatar_url: str | None,
) -> str:
    payload = {
        "u": int(user_id),
        "n": str(username),
        "gn": global_name,
        "av": avatar_url,
    }
    return _serializer(secret, "session").dumps(json.dumps(payload, separators=(",", ":")))


def decode_session(secret: str, value: str, *, max_age: int) -> SessionUser | None:
    try:
        raw = _serializer(secret, "session").loads(value, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    try:
        data = json.loads(raw)
        return SessionUser(
            user_id=int(data["u"]),
            username=str(data.get("n") or ""),
            global_name=data.get("gn"),
            avatar_url=data.get("av"),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def encode_oauth_state(secret: str, *, return_to: str | None) -> str:
    payload = json.dumps({"r": return_to or ""}, separators=(",", ":"))
    return _serializer(secret, "oauth-state").dumps(payload)


def decode_oauth_state(secret: str, value: str, *, max_age: int = 600) -> dict[str, Any] | None:
    try:
        raw = _serializer(secret, "oauth-state").loads(value, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def set_session_cookie(
    resp: web.StreamResponse,
    *,
    value: str,
    ttl_seconds: int,
    secure: bool,
) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="None" if secure else "Lax",
        path="/",
    )


def clear_session_cookie(resp: web.StreamResponse, *, secure: bool) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        "",
        max_age=0,
        httponly=True,
        secure=secure,
        samesite="None" if secure else "Lax",
        path="/",
    )


def _bearer_token(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def read_session(request: web.Request, secret: str, *, max_age: int) -> SessionUser | None:
    """Resolve the signed-in user from cookie or Bearer token.

    If a stale session cookie is present but invalid, fall back to Authorization
    so GitHub Pages clients can use the fragment token in localStorage.
    """
    cookie_raw = request.cookies.get(SESSION_COOKIE)
    if cookie_raw:
        user = decode_session(secret, cookie_raw, max_age=max_age)
        if user is not None:
            return user
    bearer = _bearer_token(request)
    if bearer:
        return decode_session(secret, bearer, max_age=max_age)
    if cookie_raw:
        return decode_session(secret, cookie_raw, max_age=max_age)
    return None
