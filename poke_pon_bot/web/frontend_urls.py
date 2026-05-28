"""Public GitHub Pages URLs (separate from ``WEB_PUBLIC_URL`` API host)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def site_origin(settings: Any) -> str:
    """Best-effort marketing-site origin (``https://pokepon.org``)."""
    for origin in getattr(settings, "web_allowed_origins", ()) or ():
        o = str(origin).strip().rstrip("/")
        if not o:
            continue
        if "pokepon.org" in o or o.endswith("github.io"):
            return o
    fb = (getattr(settings, "web_frontend_url", None) or "").strip()
    if fb:
        parts = urlsplit(fb)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return "https://pokepon.org"


def shop_page_url(settings: Any) -> str:
    """Canonical shop page (``https://pokepon.org/shop/``) — never double ``/shop/``."""
    return f"{site_origin(settings).rstrip('/')}/shop/"


def trades_page_url(settings: Any, *, trade_id: int | None = None) -> str:
    """Canonical trades UI on GitHub Pages."""
    url = f"{site_origin(settings)}/trades/"
    if trade_id is not None:
        url += f"?trade={int(trade_id)}"
    return url


def duel_page_url(settings: Any, *, duel_id: int | None = None) -> str:
    """Canonical duels UI on GitHub Pages (never under ``/shop/``)."""
    url = f"{site_origin(settings).rstrip('/')}/duel/"
    if duel_id is not None:
        url += f"?duel={int(duel_id)}"
    return url
