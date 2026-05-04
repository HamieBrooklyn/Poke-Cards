"""Search the full imported TCG catalog (not user inventory)."""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.collection_search import _apply_filters

# Max rows for /catalog search text list.
CATALOG_TEXT_LIMIT = 25
# Max cards in the /catalog view pager in one session.
CATALOG_BROWSE_PAGE_CAP = 100


def _apply_rarity_tier_filter(stmt: Select, rarity_tier: str | None) -> Select:
    if not (rarity_tier and rarity_tier.strip()):
        return stmt
    stmt = stmt.join(RarityClass, Card.rarity_class_id == RarityClass.id)
    q = f"%{rarity_tier.strip().lower()}%"
    return stmt.where(func.lower(RarityClass.code).like(q))


def _apply_set_supertype_filters(
    stmt: Select,
    *,
    set_code: str | None,
    set_name: str | None,
    supertype: str | None,
) -> Select:
    if set_code and set_code.strip():
        q = f"%{set_code.strip().lower()}%"
        stmt = stmt.where(func.lower(Card.set_code).like(q))
    if set_name and set_name.strip():
        q = f"%{set_name.strip().lower()}%"
        stmt = stmt.where(func.lower(Card.set_name).like(q))
    if supertype and supertype.strip():
        q = f"%{supertype.strip().lower()}%"
        stmt = stmt.where(
            Card.supertype.isnot(None),
            func.lower(Card.supertype).like(q),
        )
    return stmt


def _build_catalog_select(
    *,
    name_contains: str | None,
    rarity_contains: str | None,
    pokedex: int | None,
    set_code: str | None,
    set_name: str | None,
    supertype: str | None,
    rarity_tier: str | None,
    tcg_card_id: str | None = None,
) -> Select:
    """Primary filtered ``SELECT cards.*`` (may join ``rarity_classes``)."""
    stmt: Select = select(Card)
    if tcg_card_id and tcg_card_id.strip():
        stmt = stmt.where(Card.tcg_card_id == tcg_card_id.strip())
    stmt = _apply_filters(
        stmt,
        name_contains=name_contains,
        rarity_contains=rarity_contains,
        pokedex=pokedex,
    )
    stmt = _apply_set_supertype_filters(
        stmt,
        set_code=set_code,
        set_name=set_name,
        supertype=supertype,
    )
    return _apply_rarity_tier_filter(stmt, rarity_tier)


def any_catalog_content_filter_set(
    *,
    name_contains: str | None,
    rarity_contains: str | None,
    pokedex: int | None,
    set_code: str | None,
    set_name: str | None,
    supertype: str | None,
    rarity_tier: str | None,
    tcg_card_id: str | None = None,
) -> bool:
    """At least one search constraint (excludes **slot** — use with catalog view separately)."""
    if tcg_card_id and tcg_card_id.strip():
        return True
    if pokedex is not None:
        return True
    if name_contains and name_contains.strip():
        return True
    if rarity_contains and rarity_contains.strip():
        return True
    if set_code and set_code.strip():
        return True
    if set_name and set_name.strip():
        return True
    if supertype and supertype.strip():
        return True
    if rarity_tier and rarity_tier.strip():
        return True
    return False


def any_catalog_filter_set(
    *,
    name_contains: str | None,
    rarity_contains: str | None,
    pokedex: int | None,
    set_code: str | None,
    set_name: str | None,
    supertype: str | None,
    rarity_tier: str | None,
    slot: int | None,
    tcg_card_id: str | None = None,
) -> bool:
    """At least one of content filters or **slot** (for slash validation)."""
    if any_catalog_content_filter_set(
        name_contains=name_contains,
        rarity_contains=rarity_contains,
        pokedex=pokedex,
        set_code=set_code,
        set_name=set_name,
        supertype=supertype,
        rarity_tier=rarity_tier,
        tcg_card_id=tcg_card_id,
    ):
        return True
    if slot is not None:
        return True
    return False


async def search_catalog(
    session: AsyncSession,
    *,
    name_contains: str | None = None,
    rarity_contains: str | None = None,
    pokedex: int | None = None,
    set_code: str | None = None,
    set_name: str | None = None,
    supertype: str | None = None,
    rarity_tier: str | None = None,
    slot: int | None = None,
    page_limit: int = CATALOG_TEXT_LIMIT,
    tcg_card_id: str | None = None,
) -> tuple[list[Card], int]:
    """Return matching cards and total count (``slot`` = 1-based, ordered by ``Card.id``)."""
    base = _build_catalog_select(
        name_contains=name_contains,
        rarity_contains=rarity_contains,
        pokedex=pokedex,
        set_code=set_code,
        set_name=set_name,
        supertype=supertype,
        rarity_tier=rarity_tier,
        tcg_card_id=tcg_card_id,
    )
    subq = base.subquery()
    count_stmt = select(func.count()).select_from(subq)
    total = int(await session.scalar(count_stmt) or 0)
    if total == 0:
        return [], 0

    ordered = base.order_by(Card.id.asc())
    if slot is not None:
        if slot < 1 or slot > total:
            return [], total
        ordered = ordered.offset(int(slot) - 1).limit(1)
        res = await session.execute(ordered)
        one = res.scalar_one_or_none()
        return ([one] if one is not None else []), total

    cap = max(1, min(int(page_limit), CATALOG_BROWSE_PAGE_CAP))
    ordered = base.order_by(Card.id.asc()).limit(cap)
    res = await session.execute(ordered)
    return list(res.scalars().all()), total
