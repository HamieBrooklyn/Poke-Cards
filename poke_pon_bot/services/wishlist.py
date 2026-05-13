"""Wishlist service — add/remove/check/query helpers for user_wishlists."""

from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.wishlist import UserWishlist

_LOG = logging.getLogger(__name__)

WISHLIST_CAP = 50


async def add_wishlist(
    session: AsyncSession,
    *,
    discord_user_id: int,
    card_id: int,
) -> bool:
    """Add a card to the user's wishlist.  Returns True if newly added, False if already present or at cap."""
    count_stmt = (
        select(UserWishlist.id)
        .where(UserWishlist.discord_user_id == discord_user_id)
    )
    res = await session.execute(count_stmt)
    if len(res.all()) >= WISHLIST_CAP:
        return False

    stmt = (
        sqlite_insert(UserWishlist)
        .values(discord_user_id=discord_user_id, card_id=card_id)
        .on_conflict_do_nothing()
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


async def remove_wishlist(
    session: AsyncSession,
    *,
    discord_user_id: int,
    card_id: int,
) -> bool:
    """Remove a card from the user's wishlist.  Returns True if removed."""
    stmt = (
        delete(UserWishlist)
        .where(UserWishlist.discord_user_id == discord_user_id)
        .where(UserWishlist.card_id == card_id)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


async def is_wishlisted(
    session: AsyncSession,
    *,
    discord_user_id: int,
    card_id: int,
) -> bool:
    """Check whether a card is on a user's wishlist."""
    stmt = (
        select(UserWishlist.id)
        .where(UserWishlist.discord_user_id == discord_user_id)
        .where(UserWishlist.card_id == card_id)
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    return row is not None


async def wishlist_user_ids_for_cards(
    session: AsyncSession,
    card_ids: Sequence[int],
    *,
    exclude_user_id: int | None = None,
) -> dict[int, list[int]]:
    """Return {card_id: [discord_user_id, ...]} for users who wishlisted any of the given cards."""
    if not card_ids:
        return {}
    stmt = select(UserWishlist.card_id, UserWishlist.discord_user_id).where(
        UserWishlist.card_id.in_(card_ids)
    )
    if exclude_user_id is not None:
        stmt = stmt.where(UserWishlist.discord_user_id != exclude_user_id)
    rows = (await session.execute(stmt)).all()
    out: dict[int, list[int]] = {}
    for cid, uid in rows:
        out.setdefault(cid, []).append(uid)
    return out


async def user_wishlist_entries(
    session: AsyncSession,
    discord_user_id: int,
    *,
    limit: int = WISHLIST_CAP,
    offset: int = 0,
) -> tuple[list[tuple[UserWishlist, Card]], int]:
    """Paginated wishlist entries with joined Card data.  Returns (rows, total)."""
    count_stmt = (
        select(UserWishlist.id)
        .where(UserWishlist.discord_user_id == discord_user_id)
    )
    total = len((await session.execute(count_stmt)).all())

    stmt = (
        select(UserWishlist, Card)
        .join(Card, UserWishlist.card_id == Card.id)
        .where(UserWishlist.discord_user_id == discord_user_id)
        .order_by(UserWishlist.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [(w, c) for w, c in rows], total
