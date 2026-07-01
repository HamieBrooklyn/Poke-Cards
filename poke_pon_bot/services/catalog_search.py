"""Search the full imported TCG catalog (not user inventory)."""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.collection_search import _apply_filters
from poke_pon_bot.services.excluded_sets import excluded_set_clause

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
    card_ids: list[int] | None = None,
) -> Select:
    """Primary filtered ``SELECT cards.*`` (may join ``rarity_classes``)."""
    stmt: Select = select(Card).where(excluded_set_clause(Card.set_code))
    if card_ids:
        stmt = stmt.where(Card.id.in_([int(x) for x in card_ids]))
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


def _catalog_browse_order(sort: str):
    key = (sort or "name").strip().lower()
    if key in ("hp", "hp_desc"):
        return Card.hp.desc().nullslast() if key == "hp_desc" else Card.hp.asc().nullslast()
    if key in ("rarity", "rarity_desc"):
        col = RarityClass.sort_order
        return col.desc() if key == "rarity_desc" else col.asc()
    if key == "name_desc":
        return Card.name.desc()
    return Card.name.asc()


async def browse_catalog(
    session: AsyncSession,
    *,
    q: str | None = None,
    set_code: str | None = None,
    supertype: str | None = None,
    rarity_tier: str | None = None,
    pokedex: int | None = None,
    sort: str = "name",
    page: int = 1,
    page_size: int = 60,
    card_ids: list[int] | None = None,
) -> tuple[list[tuple[Card, RarityClass | None]], int]:
    """Paginated catalog browse for the public website API."""
    base = _build_catalog_select(
        name_contains=q,
        rarity_contains=None,
        pokedex=pokedex,
        set_code=set_code,
        set_name=None,
        supertype=supertype,
        rarity_tier=rarity_tier,
        card_ids=card_ids,
    )
    subq = base.subquery()
    total = int(await session.scalar(select(func.count()).select_from(subq)) or 0)
    if total == 0:
        return [], 0

    page = max(1, int(page))
    page_size = max(1, min(int(page_size), CATALOG_BROWSE_PAGE_CAP))
    offset = (page - 1) * page_size

    ordered = base
    if (sort or "").strip().lower().startswith("rarity"):
        ordered = ordered.join(
            RarityClass, Card.rarity_class_id == RarityClass.id, isouter=True
        )
    ordered = ordered.order_by(_catalog_browse_order(sort), Card.id.asc())
    cards = list(
        (await session.execute(ordered.offset(offset).limit(page_size))).scalars().all()
    )
    if not cards:
        return [], total

    card_ids = [int(c.id) for c in cards]
    rarity_rows = (
        await session.execute(
            select(Card.id, RarityClass)
            .join(RarityClass, Card.rarity_class_id == RarityClass.id, isouter=True)
            .where(Card.id.in_(card_ids))
        )
    ).all()
    rarity_by_id = {int(cid): rarity for cid, rarity in rarity_rows}
    return [(card, rarity_by_id.get(int(card.id))) for card in cards], total


async def catalog_facets(session: AsyncSession) -> dict[str, list]:
    """Distinct filter values for the public catalog UI (Pokédex facet controls)."""
    set_rows = (
        await session.execute(
            select(Card.set_code, Card.set_name, func.count(Card.id))
            .where(excluded_set_clause(Card.set_code), Card.set_code.isnot(None))
            .group_by(Card.set_code, Card.set_name)
            .order_by(Card.set_name.asc())
        )
    ).all()
    supertype_rows = (
        await session.execute(
            select(Card.supertype, func.count(Card.id))
            .where(excluded_set_clause(Card.set_code), Card.supertype.isnot(None))
            .group_by(Card.supertype)
            .order_by(Card.supertype.asc())
        )
    ).all()
    rarity_rows = (
        await session.execute(
            select(
                RarityClass.code,
                RarityClass.display_name,
                RarityClass.sort_order,
                func.count(Card.id),
            )
            .join(Card, Card.rarity_class_id == RarityClass.id)
            .where(excluded_set_clause(Card.set_code))
            .group_by(
                RarityClass.code,
                RarityClass.display_name,
                RarityClass.sort_order,
            )
            .order_by(RarityClass.sort_order.asc())
        )
    ).all()
    rarity_tiers = [
        {"code": code, "display_name": display_name, "card_count": int(n)}
        for code, display_name, _sort, n in rarity_rows
        if code
    ]
    return {
        "sets": [
            {
                "set_code": code,
                "set_name": name,
                "card_count": int(n),
                # Legacy aliases (older frontends)
                "code": code,
                "name": name,
            }
            for code, name, n in set_rows
            if code
        ],
        "supertypes": [
            {"supertype": st, "card_count": int(n)}
            for st, n in supertype_rows
            if st
        ],
        "rarity_tiers": rarity_tiers,
        "rarities": rarity_tiers,
    }
