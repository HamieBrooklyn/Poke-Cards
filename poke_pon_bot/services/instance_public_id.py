"""Stable, shareable per-copy ID for each owned card (inventory row)."""

from __future__ import annotations

import re
import secrets

# New IDs: 12 random bytes, URL-safe base64 without padding (always 16 chars) → 96 bits of entropy.
# Legacy: 32 lowercase hex (UUID) — still accepted for every row and for paste lookup.
#
# Collision: astronomically rare; DB enforces uniqueness and insertion retries on the vanishingly
# small chance a duplicate random draw occurs.

_LEGACY_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_NEW_16 = re.compile(r"^[-A-Za-z0-9_]{16}$")


def new_public_id() -> str:
    """16 URL-safe characters (96 bits of randomness; shorter than old 32-hex, still unguessable)."""
    return secrets.token_urlsafe(12)


def normalize_public_id(raw: str) -> str | None:
    """Return a canonical `public_id` for lookup, or ``None`` if invalid.

    Accepts **legacy** 32-hex (with optional hyphens/spaces, case-insensitive) or **new** 16-char
    base64url-style IDs (case-sensitive, as produced by the bot).
    """
    t = raw.strip()
    if not t:
        return None

    compact_hex = re.sub(r"[^0-9a-f]", "", t.lower())
    if len(compact_hex) == 32 and _LEGACY_HEX_32.fullmatch(compact_hex):
        return compact_hex

    if _NEW_16.fullmatch(t):
        return t
    return None


def compact_public_id_for_line(public_id: str, *, max_chars: int = 18) -> str:
    """Shorten Card ID for tight single-line chat layouts (middle ellipsis). Does not affect lookups."""
    if len(public_id) <= max_chars:
        return public_id
    inner = max_chars - 1
    left = inner // 2
    right = inner - left
    return f"{public_id[:left]}…{public_id[-right:]}"
