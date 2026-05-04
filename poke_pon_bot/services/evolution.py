"""Evolve an owned copy into the linked catalog card (Pokedollars cost)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

from sqlalchemy import or_, select

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from poke_pon_bot.services.wallet import WalletService

# Tuned to be "expensive" vs daily 100–200 ₽; rarer tiers and repeat evolutions scale up.
_BASE = 500
_TIER_PER_SORT_ORDER = 150
_STAGES_GROWTH = 1.48


_SET_RANK_RE = re.compile(r"^[rz]?sv(\d+)(?:pt(\d+))?$", re.IGNORECASE)


def _norm_set_rank_score(code: str) -> int:
    """Rough Scarlet/Violet-era ordering so ``sv10`` sorts after ``sv9`` (string sort alone breaks that)."""
    c = (code or "").strip().lower()
    if c == "svp":
        return 150_000
    if c == "sve":
        return -1000
    m = _SET_RANK_RE.fullmatch(c)
    if not m:
        return 0
    maj = int(m.group(1))
    sub = int(m.group(2) or 0)
    return maj * 100 + sub


def _evolves_from_matches_prey(evolved_from: str, prey_card_name: str) -> bool:
    """True if catalog ``evolvesFrom`` refers to this printing’s Pokémon (handles ``Eevee`` vs ``Eevee ex``)."""
    ef = (evolved_from or "").strip().casefold()
    pn = (prey_card_name or "").strip().casefold()
    if not ef or not pn:
        return False
    if ef == pn:
        return True
    return pn.startswith(ef + " ") or pn.startswith(ef + "-") or ef.startswith(pn + " ") or ef.startswith(pn + "-")


async def resolve_evolution_targets(session: "AsyncSession", card: Card) -> list[Card]:
    """Catalog cards this printing may evolve into.

    Uses API ``evolvesTo`` names first (same-set printings, then cross-set fallback when that species
    does not exist in the source set — e.g. Eevee in Obsidian Flames lists eight branches but only two
    are printed there). Finally supplements from ``evolvesFrom`` within the same set.
    """
    targets: list[Card] = []
    seen: set[int] = set()

    def push(c: Card) -> None:
        if c.id == card.id or c.id in seen:
            return
        seen.add(c.id)
        targets.append(c)

    names = card.evolves_to_names
    if isinstance(names, list):
        for tname in names:
            if not isinstance(tname, str):
                continue
            clean = tname.strip()
            if not clean:
                continue
            # API lists base species ("Vaporeon"); catalog rows may be "Vaporeon ex", "Vaporeon V", …
            match_name = or_(Card.name == clean, Card.name.startswith(f"{clean} "))
            stmt = (
                select(Card)
                .where(Card.set_code == card.set_code, match_name)
                .order_by(Card.collector_number.asc())
            )
            same_rows = list((await session.execute(stmt)).scalars().all())
            if same_rows:
                for row in same_rows:
                    push(row)
                continue
            stmt_any = select(Card).where(match_name)
            pool = list((await session.execute(stmt_any)).scalars().all())
            if not pool:
                continue
            pool.sort(key=lambda c: (_norm_set_rank_score(c.set_code), c.collector_number), reverse=True)
            push(pool[0])

    if (card.name or "").strip():
        stmt = (
            select(Card)
            .where(Card.set_code == card.set_code)
            .order_by(Card.collector_number.asc())
        )
        for t in (await session.execute(stmt)).scalars():
            if t.id == card.id:
                continue
            if not _evolves_from_matches_prey(t.evolves_from or "", card.name or ""):
                continue
            # Do not compare collector numbers: branching Basics (e.g. Eevee) often appear after
            # their eeveelutions in set numbering, which would wrongly drop valid targets.
            push(t)

    return targets


@dataclass(frozen=True)
class EvolutionQuote:
    cost: int
    target_name: str
    current_stages: int


def evolution_cost_pokedollars(
    *,
    rarity_sort_order: int,
    current_evolution_stages: int,
) -> int:
    """Cost to evolve *from* a card of this printed tier, given how many evolutions the copy already has."""
    sort = max(1, int(rarity_sort_order))
    stages = max(0, int(current_evolution_stages))
    raw = float(_BASE + sort * _TIER_PER_SORT_ORDER) * (_STAGES_GROWTH**stages)
    return int(min(raw, 9_999_999))


def quote_evolution(
    card: Card,
    rarity: RarityClass,
    instance_evolution_stages: int,
    target: Card,
) -> EvolutionQuote:
    c = evolution_cost_pokedollars(
        rarity_sort_order=rarity.sort_order,
        current_evolution_stages=instance_evolution_stages,
    )
    return EvolutionQuote(
        cost=c,
        target_name=target.name,
        current_stages=instance_evolution_stages,
    )


@dataclass(frozen=True)
class EvolutionSuccess:
    """Committed evolution (same inventory row, new ``card_id``)."""

    inst: UserCardInstance
    new_card: Card
    cost: int
    new_balance: int
    before_name: str


async def run_collection_evolution(
    session: "AsyncSession",
    wallet: "WalletService",
    user_id: int,
    instance_id: int,
    *,
    target_card_id: int | None = None,
) -> Union[EvolutionSuccess, str]:
    """Apply one evolution for ``instance_id`` owned by ``user_id``. Return ``EvolutionSuccess`` or error string.

    For branching lines (e.g. Eevee), pass ``target_card_id`` chosen by the player. If there is exactly one valid
    target, ``target_card_id`` may be omitted.
    """
    from poke_pon_bot.services.wallet import InsufficientPokedollarsError, format_pokedollars

    inst = await session.get(UserCardInstance, instance_id)
    if inst is None or inst.discord_user_id != user_id:
        return "That copy is no longer in your collection."
    card = await session.get(Card, inst.card_id)
    if card is None:
        return "This card can’t be evolved (no next stage in the catalog for this set)."
    valid_targets = await resolve_evolution_targets(session, card)
    allowed = {t.id for t in valid_targets}
    if not allowed:
        return "This card can’t be evolved (no next stage in the catalog for this set)."
    if target_card_id is not None:
        if target_card_id not in allowed:
            return "That evolution isn’t available for this printing."
        chosen_id = target_card_id
    elif len(allowed) == 1:
        chosen_id = next(iter(allowed))
    else:
        return "Pick which Pokémon to evolve into."
    target = await session.get(Card, chosen_id)
    if target is None:
        return "Evolution data is out of date — re-run **catalog sync**."
    rc = await session.get(RarityClass, card.rarity_class_id)
    if rc is None:
        return "Rarity data is missing. Try again later."
    q = quote_evolution(card, rc, inst.evolution_stages, target)
    before_name = card.name
    try:
        new_balance = await wallet.try_debit(session, user_id, q.cost)
    except InsufficientPokedollarsError:
        return f"You need **{format_pokedollars(q.cost)}** to evolve."
    inst.card_id = target.id
    inst.evolution_stages = inst.evolution_stages + 1
    await session.commit()
    await session.refresh(inst)
    new_card = await session.get(Card, inst.card_id)
    if new_card is None:
        return "Evolved, but the new card is missing from the database — re-sync the catalog."
    return EvolutionSuccess(
        inst=inst,
        new_card=new_card,
        cost=q.cost,
        new_balance=new_balance,
        before_name=before_name,
    )
