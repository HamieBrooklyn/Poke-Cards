"""Evolution-line sections for owned-collection name search (web + trades picker)."""

from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

from sqlalchemy import Integer, desc, func, or_, select

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.collection_visibility import user_instance_not_in_active_auction
from poke_pon_bot.services.collection_search import (
    collection_text_search_clause,
    collection_text_search_negated_clause,
)
from poke_pon_bot.services.evolution import (
    resolve_evolution_targets,
    resolve_pre_evolution_sources,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_SEED_CARD_CAP = 80
_SECTION_LIMIT = 60


def _max_attack_damage(attacks: Any) -> int:
    if not isinstance(attacks, list):
        return 0
    best = 0
    for atk in attacks:
        if not isinstance(atk, dict):
            continue
        raw = atk.get("damage")
        if raw is None:
            continue
        digits: list[str] = []
        for ch in str(raw):
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            best = max(best, int("".join(digits)))
    return best


def _species_label(card_name: str) -> str:
    return (card_name or "").strip()


def _names_match_clause(names: set[str]):
    parts = []
    for raw in names:
        clean = raw.strip()
        if not clean:
            continue
        parts.append(or_(Card.name == clean, Card.name.startswith(f"{clean} ")))
    if not parts:
        return None
    return or_(*parts)


async def _seed_cards_for_query(
    session: "AsyncSession",
    *,
    discord_user_id: int,
    name_contains: str,
    favorited_only: bool,
) -> list[Card]:
    q = name_contains.strip().lower()
    if not q:
        return []
    stmt = (
        select(Card)
        .join(UserCardInstance, UserCardInstance.card_id == Card.id)
        .where(
            UserCardInstance.discord_user_id == discord_user_id,
            user_instance_not_in_active_auction(),
            collection_text_search_clause(q),
        )
        .order_by(desc(UserCardInstance.obtained_at))
    )
    if favorited_only:
        stmt = stmt.where(UserCardInstance.is_favorite.is_(True))
    rows = list((await session.execute(stmt.limit(_SEED_CARD_CAP * 3))).scalars().all())
    seen: set[int] = set()
    out: list[Card] = []
    for card in rows:
        if card.id in seen:
            continue
        seen.add(card.id)
        out.append(card)
        if len(out) >= _SEED_CARD_CAP:
            break
    return out


async def _collect_line_species(
    session: "AsyncSession", seed_cards: list[Card]
) -> tuple[set[str], set[str]]:
    evolves_into: set[str] = set()
    pre_evolves: set[str] = set()
    for card in seed_cards:
        for target in await resolve_evolution_targets(session, card):
            label = _species_label(target.name or "")
            if label:
                evolves_into.add(label)
        for source in await resolve_pre_evolution_sources(session, card):
            label = _species_label(source.name or "")
            if label:
                pre_evolves.add(label)
    return evolves_into, pre_evolves


def _order_rows(
    rows: list[tuple[UserCardInstance, Card, RarityClass | None]],
    *,
    sort: str,
) -> list[tuple[UserCardInstance, Card, RarityClass | None]]:
    if sort == "rarity":
        return sorted(
            rows,
            key=lambda r: (
                -(r[2].sort_order if r[2] is not None else 0),
                -(r[0].obtained_at.timestamp() if r[0].obtained_at else 0.0),
            ),
        )
    if sort == "hp":
        def hp_key(r: tuple[UserCardInstance, Card, RarityClass | None]) -> tuple[int, float]:
            m = re.match(r"^(\d+)", (r[1].hp or "").strip())
            hp = int(m.group(1)) if m else 0
            ts = r[0].obtained_at.timestamp() if r[0].obtained_at else 0.0
            return (-hp, -ts)

        return sorted(rows, key=hp_key)
    if sort == "damage":
        return sorted(
            rows,
            key=lambda r: (
                -_max_attack_damage(r[1].attacks),
                -(r[0].obtained_at.timestamp() if r[0].obtained_at else 0.0),
            ),
        )
    return sorted(
        rows,
        key=lambda r: -(r[0].obtained_at.timestamp() if r[0].obtained_at else 0.0),
    )


async def _owned_rows_for_species(
    session: "AsyncSession",
    *,
    discord_user_id: int,
    species_names: set[str],
    name_contains: str,
    favorited_only: bool,
    sort: str,
    limit: int,
) -> list[tuple[UserCardInstance, Card, RarityClass | None]]:
    clause = _names_match_clause(species_names)
    if clause is None:
        return []
    q = name_contains.strip().lower()
    stmt = (
        select(UserCardInstance, Card, RarityClass)
        .join(Card, Card.id == UserCardInstance.card_id)
        .join(RarityClass, RarityClass.id == Card.rarity_class_id, isouter=True)
        .where(
            UserCardInstance.discord_user_id == discord_user_id,
            user_instance_not_in_active_auction(),
            clause,
        )
    )
    if q:
        exclude_direct = collection_text_search_negated_clause(q)
        if exclude_direct is not None:
            stmt = stmt.where(exclude_direct)
    if favorited_only:
        stmt = stmt.where(UserCardInstance.is_favorite.is_(True))
    rows = list((await session.execute(stmt.limit(max(limit * 4, 120)))).all())
    ordered = _order_rows(rows, sort=sort)
    return ordered[:limit]


async def build_evolution_line_sections(
    session: "AsyncSession",
    *,
    discord_user_id: int,
    name_contains: str,
    favorited_only: bool,
    sort: str,
    limit_per_section: int = _SECTION_LIMIT,
) -> list[dict[str, Any]]:
    """Sections of owned copies in the evolution line of search hits (excludes direct name matches)."""
    q = (name_contains or "").strip()
    if not q:
        return []

    seed_cards = await _seed_cards_for_query(
        session,
        discord_user_id=discord_user_id,
        name_contains=q,
        favorited_only=favorited_only,
    )
    if not seed_cards:
        return []

    evolves_into, pre_evolves = await _collect_line_species(session, seed_cards)
    cap = max(1, min(int(limit_per_section), _SECTION_LIMIT))
    sections: list[dict[str, Any]] = []

    if evolves_into:
        rows = await _owned_rows_for_species(
            session,
            discord_user_id=discord_user_id,
            species_names=evolves_into,
            name_contains=q,
            favorited_only=favorited_only,
            sort=sort,
            limit=cap,
        )
        if rows:
            sections.append(
                {
                    "key": "evolves_into",
                    "label": "Evolves into",
                    "total": len(rows),
                    "rows": rows,
                }
            )

    if pre_evolves:
        rows = await _owned_rows_for_species(
            session,
            discord_user_id=discord_user_id,
            species_names=pre_evolves,
            name_contains=q,
            favorited_only=favorited_only,
            sort=sort,
            limit=cap,
        )
        if rows:
            sections.append(
                {
                    "key": "pre_evolves_from",
                    "label": "Pre-evolves from",
                    "total": len(rows),
                    "rows": rows,
                }
            )

    return sections
