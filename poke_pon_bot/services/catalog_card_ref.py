"""Resolve a catalog printing from a dev/ops reference string."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card


def looks_like_catalog_card_ref(ref: str) -> bool:
    """True if *ref* is likely a catalog id (``sv1-112``) or numeric row id, not a name search."""
    raw = (ref or "").strip()
    if not raw:
        return False
    if raw.isdigit():
        return True
    return "-" in raw


async def resolve_catalog_card_ref(session: AsyncSession, ref: str) -> Card | None:
    """Look up a catalog row by ``tcg_card_id`` (e.g. ``xy11-66``) or internal numeric ``id``."""
    raw = (ref or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return await session.get(Card, int(raw))
    card = await session.scalar(select(Card).where(Card.tcg_card_id == raw))
    if card is not None:
        return card
    return await session.scalar(
        select(Card).where(func.lower(Card.tcg_card_id) == raw.casefold())
    )
