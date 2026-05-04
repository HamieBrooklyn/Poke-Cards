"""Load and save a user’s ordered combat deck (owned instances only)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.combat_deck import UserCombatDeck
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.instance_public_id import normalize_public_id

MIN_DECK = 1
MAX_DECK = 6


def _parse_hp(hp_raw: str | None) -> int:
    if not hp_raw:
        return 0
    s = str(hp_raw).strip()
    acc = []
    for ch in s:
        if ch.isdigit():
            acc.append(ch)
        elif acc:
            break
    return int("".join(acc)) if acc else 0


def _is_eligible_pokemon(card: Card) -> bool:
    if (card.supertype or "").strip() != "Pokémon":
        return False
    return _parse_hp(card.hp) > 0


async def get_saved_instance_ids(session: AsyncSession, discord_user_id: int) -> list[int] | None:
    row = await session.get(UserCombatDeck, discord_user_id)
    if row is None:
        return None
    raw = row.instance_ids
    if not isinstance(raw, list) or not raw:
        return None
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            return None
    return out


async def load_fighters_ordered(
    session: AsyncSession,
    discord_user_id: int,
    ordered_instance_ids: list[int],
) -> list[tuple[UserCardInstance, Card]] | str:
    """Return pairs in order, or an error string."""
    if len(ordered_instance_ids) < MIN_DECK or len(ordered_instance_ids) > MAX_DECK:
        return f"Deck must have **{MIN_DECK}**–**{MAX_DECK}** Pokémon."
    if len(set(ordered_instance_ids)) != len(ordered_instance_ids):
        return "Deck can’t include the same physical card twice."
    pairs: list[tuple[UserCardInstance, Card]] = []
    for iid in ordered_instance_ids:
        inst = await session.get(UserCardInstance, iid)
        if inst is None or inst.discord_user_id != discord_user_id:
            return "One or more cards aren’t in your collection."
        card = await session.get(Card, inst.card_id)
        if card is None:
            return "Catalog data missing for a card — re-sync."
        if not _is_eligible_pokemon(card):
            return f"**{card.name}** isn’t a Pokémon with HP in the catalog — remove it from your deck."
        pairs.append((inst, card))
    return pairs


async def strip_instances_from_deck(session: AsyncSession, discord_user_id: int, removed_ids: set[int]) -> None:
    """Drop ``removed_ids`` from the user’s saved deck; delete the deck row if too few Pokémon remain."""
    if not removed_ids:
        return
    row = await session.get(UserCombatDeck, discord_user_id)
    if row is None:
        return
    raw = row.instance_ids
    if not isinstance(raw, list) or not raw:
        return
    keep: list[int] = []
    for x in raw:
        try:
            iid = int(x)
        except (TypeError, ValueError):
            continue
        if iid not in removed_ids:
            keep.append(iid)
    if len(keep) < MIN_DECK:
        await session.delete(row)
        return
    if len(keep) != len(raw):
        row.instance_ids = keep


async def set_deck_from_public_ids(
    session: AsyncSession,
    discord_user_id: int,
    public_ids: list[str],
) -> str | None:
    """Persist deck; return error message or ``None`` on success."""
    if len(public_ids) < MIN_DECK or len(public_ids) > MAX_DECK:
        return f"Give **{MIN_DECK}**–**{MAX_DECK}** Card IDs (space-separated)."
    if len(set(public_ids)) != len(public_ids):
        return "Duplicate Card ID in list."
    resolved: list[int] = []
    for raw in public_ids:
        n = normalize_public_id(raw.strip())
        if n is None:
            return f"Invalid Card ID: `{raw}`."
        row = await session.execute(
            select(UserCardInstance.id)
            .where(
                UserCardInstance.discord_user_id == discord_user_id,
                UserCardInstance.public_id == n,
            )
            .limit(1),
        )
        first = row.first()
        if first is None:
            return f"You don’t own a card with Card ID `{raw}`."
        iid = int(first[0])
        if iid in resolved:
            return "Same instance listed twice."
        resolved.append(iid)
    check = await load_fighters_ordered(session, discord_user_id, resolved)
    if isinstance(check, str):
        return check
    existing = await session.get(UserCombatDeck, discord_user_id)
    if existing is None:
        session.add(UserCombatDeck(discord_user_id=discord_user_id, instance_ids=resolved))
    else:
        existing.instance_ids = resolved
    return None
