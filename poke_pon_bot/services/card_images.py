"""Resolve catalog image URLs for API responses and Discord embeds."""

from __future__ import annotations

from poke_pon_bot.services.manual_cards import resolve_public_image_url


def card_image_urls(
    card: object,
    *,
    web_public_url: str | None,
) -> tuple[str, str]:
    """Return ``(small, large)`` URLs, prefixing ``/static/`` paths when configured."""
    small = str(getattr(card, "image_small_url", "") or "")
    large = str(getattr(card, "image_large_url", "") or "")
    return (
        resolve_public_image_url(small, web_public_url),
        resolve_public_image_url(large, web_public_url),
    )
