"""Query helpers for searching a user's owned cards."""

from __future__ import annotations

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.instance_public_id import normalize_public_id


def _apply_filters(
    stmt: Select,
    *,
    name_contains: str | None,
    rarity_contains: str | None,
    pokedex: int | None,
) -> Select:
    if name_contains and name_contains.strip():
        q = f"%{name_contains.strip().lower()}%"
        stmt = stmt.where(func.lower(Card.name).like(q))

    if rarity_contains and rarity_contains.strip():
        r = f"%{rarity_contains.strip().lower()}%"
        stmt = stmt.where(
            Card.tcg_rarity.isnot(None),
            func.lower(Card.tcg_rarity).like(r),
        )

    if pokedex is not None:
        dex = int(pokedex)
        stmt = stmt.where(
            Card.dex_numbers.isnot(None),
            text(
                "EXISTS (SELECT 1 FROM json_each(cards.dex_numbers) "
                "WHERE CAST(json_each.value AS INTEGER) = :dex_num)"
            ).bindparams(dex_num=dex),
        )

    return stmt


def _base_stmt(discord_user_id: int) -> Select:
    return (
        select(UserCardInstance, Card)
        .join(Card, UserCardInstance.card_id == Card.id)
        .where(UserCardInstance.discord_user_id == discord_user_id)
    )


async def search_collection(
    session: AsyncSession,
    *,
    discord_user_id: int,
    name_contains: str | None = None,
    rarity_contains: str | None = None,
    pokedex: int | None = None,
    slot: int | None = None,
    page_limit: int = 25,
    page_offset: int = 0,
    public_id: str | None = None,
) -> tuple[list[tuple[UserCardInstance, Card]], int]:
    """Return matching ``(instance, card)`` rows and total match count.

    When ``public_id`` is set, only that owned copy (exact Card ID) is considered; other
    content filters and ``slot`` are ignored.

    ``slot`` is 1-based into the filtered list ordered by ``obtained_at`` descending
    (1 = most recently obtained among matches). When ``slot`` is set, at most one row is returned.

    When ``slot`` is ``None``, ``page_offset`` skips that many rows (for pagination) before
    ``page_limit`` is applied. ``page_offset`` must be non-negative; invalid values are clamped to 0.
    """
    n_pid: int | None = None
    if public_id and public_id.strip():
        n_pid = normalize_public_id(public_id)
        if n_pid is None:
            return [], 0

    if n_pid is not None:
        stmt = _base_stmt(discord_user_id).where(UserCardInstance.public_id == n_pid)
        count_stmt = (
            select(func.count())
            .select_from(UserCardInstance)
            .join(Card, UserCardInstance.card_id == Card.id)
            .where(
                UserCardInstance.discord_user_id == discord_user_id,
                UserCardInstance.public_id == n_pid,
            )
        )
    else:
        stmt = _base_stmt(discord_user_id)
        stmt = _apply_filters(
            stmt,
            name_contains=name_contains,
            rarity_contains=rarity_contains,
            pokedex=pokedex,
        )

        count_stmt = (
            select(func.count())
            .select_from(UserCardInstance)
            .join(Card, UserCardInstance.card_id == Card.id)
            .where(UserCardInstance.discord_user_id == discord_user_id)
        )
        count_stmt = _apply_filters(
            count_stmt,
            name_contains=name_contains,
            rarity_contains=rarity_contains,
            pokedex=pokedex,
        )
    total = int(await session.scalar(count_stmt) or 0)

    stmt = stmt.order_by(UserCardInstance.obtained_at.desc())

    if n_pid is not None:
        result = await session.execute(stmt.limit(1))
        return list(result.all()), total

    if slot is not None:
        if slot < 1:
            return [], total
        stmt = stmt.offset(slot - 1).limit(1)
        result = await session.execute(stmt)
        return list(result.all()), total

    safe_limit = max(1, min(page_limit, 25))
    off = max(0, int(page_offset))
    stmt = stmt.offset(off).limit(safe_limit)
    result = await session.execute(stmt)
    return list(result.all()), total


def any_filter_set(
    *,
    name_contains: str | None,
    rarity_contains: str | None,
    pokedex: int | None,
    slot: int | None,
    public_id: str | None = None,
) -> bool:
    """True if the user provided at least one search criterion."""
    if public_id and public_id.strip():
        return True
    if slot is not None:
        return True
    if pokedex is not None:
        return True
    if name_contains and name_contains.strip():
        return True
    if rarity_contains and rarity_contains.strip():
        return True
    return False
