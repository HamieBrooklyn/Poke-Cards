"""Search active card listings (join to catalog for filters)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, case, desc, func, select

from poke_pon_bot.models.auction import CardAuction
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance

from poke_pon_bot.services.collection_search import _apply_filters


def _base_auction_stmt(*, seller_discord_id: int | None, guild_id: int | None) -> Select:
    stmt = (
        select(CardAuction, UserCardInstance, Card)
        .join(UserCardInstance, CardAuction.instance_id == UserCardInstance.id)
        .join(Card, UserCardInstance.card_id == Card.id)
        .where(CardAuction.status == "active")
    )
    if seller_discord_id is not None:
        stmt = stmt.where(CardAuction.seller_discord_id == int(seller_discord_id))
    if guild_id is not None:
        stmt = stmt.where(CardAuction.guild_id == int(guild_id))
    return stmt


def any_auction_filter_set(
    *,
    name_contains: str | None,
    rarity_contains: str | None,
    pokedex: int | None,
    seller_discord_id: int | None = None,
    guild_id: int | None = None,
) -> bool:
    if seller_discord_id is not None:
        return True
    if guild_id is not None:
        return True
    if pokedex is not None:
        return True
    if name_contains and name_contains.strip():
        return True
    if rarity_contains and rarity_contains.strip():
        return True
    return False


async def search_auctions(
    session,
    *,
    name_contains: str | None = None,
    rarity_contains: str | None = None,
    pokedex: int | None = None,
    seller_discord_id: int | None = None,
    guild_id: int | None = None,
    page_limit: int = 20,
    page_offset: int = 0,
    sort: str = "newest",
    browse_all_if_no_card_filters: bool = False,
) -> tuple[list[tuple[CardAuction, UserCardInstance, Card]], int]:
    has_filters = any_auction_filter_set(
        name_contains=name_contains,
        rarity_contains=rarity_contains,
        pokedex=pokedex,
        seller_discord_id=seller_discord_id,
        guild_id=guild_id,
    )
    if not has_filters and not browse_all_if_no_card_filters:
        return [], 0

    stmt = _base_auction_stmt(seller_discord_id=seller_discord_id, guild_id=guild_id)
    stmt = _apply_filters(
        stmt,
        name_contains=name_contains,
        rarity_contains=rarity_contains,
        pokedex=pokedex,
    )
    count_stmt = (
        select(func.count())
        .select_from(CardAuction)
        .join(UserCardInstance, CardAuction.instance_id == UserCardInstance.id)
        .join(Card, UserCardInstance.card_id == Card.id)
        .where(CardAuction.status == "active")
    )
    if seller_discord_id is not None:
        count_stmt = count_stmt.where(CardAuction.seller_discord_id == int(seller_discord_id))
    if guild_id is not None:
        count_stmt = count_stmt.where(CardAuction.guild_id == int(guild_id))
    count_stmt = _apply_filters(
        count_stmt,
        name_contains=name_contains,
        rarity_contains=rarity_contains,
        pokedex=pokedex,
    )
    total = int(await session.scalar(count_stmt) or 0)

    sort_key = (sort or "newest").strip().lower()
    if sort_key == "ending":
        stmt = stmt.order_by(CardAuction.ends_at.asc())
    elif sort_key == "popular":
        from poke_pon_bot.models.auction import AuctionBid

        bid_count = (
            select(func.count(AuctionBid.id))
            .where(AuctionBid.auction_id == CardAuction.id)
            .correlate(CardAuction)
            .scalar_subquery()
        )
        stmt = stmt.order_by(bid_count.desc(), CardAuction.created_at.desc())
    else:
        now = datetime.now(UTC)
        spotlight_rank = case(
            (
                (CardAuction.spotlight_until.isnot(None))
                & (CardAuction.spotlight_until > now),
                1,
            ),
            else_=0,
        )
        stmt = stmt.order_by(desc(spotlight_rank), CardAuction.created_at.desc())

    safe_limit = max(1, min(int(page_limit), 25))
    off = max(0, int(page_offset))
    stmt = stmt.offset(off).limit(safe_limit)
    result = await session.execute(stmt)
    return list(result.all()), total
