"""Verify Top.gg v1 ``x-topgg-signature`` (HMAC SHA-256)."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Final

_DEFAULT_MAX_SKEW_SEC: Final[int] = 300


def verify_topgg_webhook_signature(
    *,
    raw_body: bytes,
    signature_header: str | None,
    webhook_secret: str,
    max_clock_skew_seconds: int = _DEFAULT_MAX_SKEW_SEC,
) -> bool:
    """
    Validate ``x-topgg-signature`` format ``t={unix},v1={hex}`` per Top.gg v1 docs.

    HMAC-SHA256 key = webhook secret, message = ``{t}.{raw_body decoded as utf-8}``.
    """
    if not signature_header or not webhook_secret:
        return False
    parts: dict[str, str] = {}
    for segment in signature_header.split(","):
        segment = segment.strip()
        if "=" not in segment:
            continue
        k, v = segment.split("=", 1)
        parts[k.strip()] = v.strip()
    ts_raw = parts.get("t")
    sig_hex = parts.get("v1")
    if not ts_raw or not sig_hex:
        return False
    try:
        ts = int(ts_raw)
    except ValueError:
        return False
    if max_clock_skew_seconds > 0:
        if abs(int(time.time()) - ts) > max_clock_skew_seconds:
            return False
    try:
        body_text = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    message = f"{ts_raw}.{body_text}".encode("utf-8")
    expected = hmac.new(webhook_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    if len(expected) != len(sig_hex):
        return False
    return hmac.compare_digest(expected.lower(), sig_hex.lower())
