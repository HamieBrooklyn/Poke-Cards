"""Wishlist: catalog cards a user wants; pings on /cd when a pack contains that printing."""

from __future__ import annotations

import logging
from collections import defaultdict

import discord
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_wishlist import UserCardWishlist
from poke_pon_bot.models.wishlist import UserWishlist

_LOG = logging.getLogger(__name__)

MAX_WISHLIST_ENTRIES = 500
WISHLIST_CAP = MAX_WISHLIST_ENTRIES


async def wishlist_count(session: AsyncSession, discord_user_id: int) -> int:
    r = await session.scalar(
        select(func.count(UserCardWishlist.id)).where(
            UserCardWishlist.discord_user_id == discord_user_id
        )
    )
    return int(r or 0)


async def is_card_wishlisted(
    session: AsyncSession, discord_user_id: int, card_id: int
) -> bool:
    row = await session.scalar(
        select(UserCardWishlist.id).where(
            UserCardWishlist.discord_user_id == discord_user_id,
            UserCardWishlist.card_id == card_id,
        ).limit(1)
    )
    return row is not None


async def is_wishlisted(
    session: AsyncSession,
    *,
    discord_user_id: int,
    card_id: int,
) -> bool:
    """Thin wrapper around ``is_card_wishlisted`` with keyword-only arguments."""
    return await is_card_wishlisted(session, discord_user_id, card_id)


async def user_wishlist_entries(
    session: AsyncSession,
    discord_user_id: int,
    *,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[tuple[UserWishlist, Card]], int]:
    """Paginated wishlist rows for the ``/wishlist`` cog."""
    total = int(
        await session.scalar(
            select(func.count(UserWishlist.id)).where(
                UserWishlist.discord_user_id == discord_user_id
            )
        )
        or 0
    )
    if total == 0:
        return [], 0
    rows = (
        await session.execute(
            select(UserWishlist, Card)
            .join(Card, UserWishlist.card_id == Card.id)
            .where(UserWishlist.discord_user_id == discord_user_id)
            .order_by(UserWishlist.created_at.desc(), UserWishlist.id.desc())
            .offset(max(0, int(offset)))
            .limit(max(1, int(limit)))
        )
    ).all()
    return list(rows), total


async def list_wishlist_card_ids(
    session: AsyncSession,
    discord_user_id: int,
    *,
    max_ids: int = MAX_WISHLIST_ENTRIES,
) -> tuple[list[int], int]:
    """Return wishlisted catalog card ids (oldest first) and total count before the cap."""
    total = await wishlist_count(session, discord_user_id)
    if total == 0:
        return [], 0
    cap = max(1, min(int(max_ids), MAX_WISHLIST_ENTRIES))
    rows = (
        await session.execute(
            select(UserCardWishlist.card_id)
            .where(UserCardWishlist.discord_user_id == discord_user_id)
            .order_by(UserCardWishlist.created_at.asc(), UserCardWishlist.id.asc())
            .limit(cap)
        )
    ).all()
    return [int(r[0]) for r in rows], total


async def add_wishlist(
    session: AsyncSession,
    *,
    discord_user_id: int,
    card_id: int,
) -> bool:
    """Insert wishlist row if absent. Returns ``True`` if inserted, ``False`` if duplicate or at cap."""
    if await is_card_wishlisted(session, discord_user_id, card_id):
        return False
    n = await wishlist_count(session, discord_user_id)
    if n >= MAX_WISHLIST_ENTRIES:
        return False
    session.add(UserCardWishlist(discord_user_id=discord_user_id, card_id=card_id))
    return True


async def remove_wishlist(
    session: AsyncSession,
    *,
    discord_user_id: int,
    card_id: int,
) -> None:
    """Remove wishlist row if present. Does not commit."""
    for model in (UserWishlist, UserCardWishlist):
        result = await session.execute(
            select(model)
            .where(
                model.discord_user_id == discord_user_id,
                model.card_id == card_id,
            )
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            await session.delete(existing)


async def wishlist_user_ids_for_cards(
    session: AsyncSession,
    card_ids: list[int],
    *,
    exclude_user_id: int | None = None,
) -> dict[int, list[int]]:
    """Map ``card_id`` → Discord user ids who starred that printing (optional obtainer excluded)."""
    if not card_ids:
        return {}
    rows = (
        await session.execute(
            select(UserCardWishlist.card_id, UserCardWishlist.discord_user_id).where(
                UserCardWishlist.card_id.in_(card_ids)
            )
        )
    ).all()
    m: dict[int, list[int]] = defaultdict(list)
    for cid, uid in rows:
        uid_i = int(uid)
        if exclude_user_id is not None and uid_i == exclude_user_id:
            continue
        m[int(cid)].append(uid_i)
    return dict(m)


async def toggle_wishlist(
    session: AsyncSession, discord_user_id: int, card_id: int
) -> tuple[bool, str]:
    """Returns (now_on_wishlist, user_message). Does not commit."""
    result = await session.execute(
        select(UserCardWishlist)
        .where(
            UserCardWishlist.discord_user_id == discord_user_id,
            UserCardWishlist.card_id == card_id,
        )
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        await session.delete(existing)
        return False, "Removed this printing from your **wishlist**."

    n = await wishlist_count(session, discord_user_id)
    if n >= MAX_WISHLIST_ENTRIES:
        return False, (
            f"Wishlist full (**{MAX_WISHLIST_ENTRIES}** max). Remove some stars on **catalog** views first."
        )

    session.add(UserCardWishlist(discord_user_id=discord_user_id, card_id=card_id))
    return True, (
        "**Starred** — you'll get pinged on **public** **`/cd`** packs in a server you're in when this printing shows up."
    )


async def pack_wishlist_mention_suffix(
    session: AsyncSession,
    guild: discord.Guild | None,
    opener_discord_id: int,
    pack_cards: list[Card],
) -> str:
    """Extra lines for /cd content when guild members wished for a card in this pack."""
    if guild is None or not pack_cards:
        return ""

    card_ids = {c.id for c in pack_cards}
    if not card_ids:
        return ""

    stmt = (
        select(UserCardWishlist.discord_user_id)
        .where(UserCardWishlist.card_id.in_(card_ids))
        .distinct()
    )
    rows = (await session.execute(stmt)).all()
    candidate_ids = [int(r[0]) for r in rows]
    if not candidate_ids:
        return ""

    member_ids = {m.id for m in guild.members if not m.bot}
    pings: list[str] = []
    for uid in candidate_ids:
        if uid == opener_discord_id:
            continue
        if uid not in member_ids:
            continue
        pings.append(f"<@{uid}>")

    if not pings:
        return ""

    return (
        "\n**Wishlist:** "
        + " ".join(pings)
        + " — _someone wants a card in this pack._"
    )
