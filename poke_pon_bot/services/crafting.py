"""Craft unopened packs from item + trainer cards."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_series import CardSeries
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.pack_instance import UserPackInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.card_roles import (
    CRAFT_ITEM_COUNT,
    CRAFT_TRAINER_MAX_USES,
    apply_new_instance_craft_uses,
    is_craft_trainer_card,
    is_item_card,
)
from poke_pon_bot.services.collection_sell import collection_sell_block_reason
from poke_pon_bot.services.collection_visibility import user_instance_not_in_active_auction
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.services.instance_public_id import new_public_id, normalize_public_id
from poke_pon_bot.services.pack_series_loader import crystal_price_for_top_rarity
from poke_pon_bot.services.packs import PackService


@dataclass(frozen=True)
class CraftSuccess:
    pack: UserPackInstance
    series: CardSeries
    trainer_name: str
    trainer_uses_remaining: int | None
    consumed_item_ids: list[str]
    consumed_trainer_id: str
    pack_tier_rarity: str


async def _load_owned_row(
    session: AsyncSession,
    *,
    discord_user_id: int,
    public_id: str,
) -> tuple[UserCardInstance, Card] | None:
    pid = normalize_public_id(public_id)
    if pid is None:
        return None
    row = await session.execute(
        select(UserCardInstance, Card)
        .join(Card, Card.id == UserCardInstance.card_id)
        .where(
            UserCardInstance.discord_user_id == discord_user_id,
            UserCardInstance.public_id == pid,
            user_instance_not_in_active_auction(),
        )
    )
    first = row.first()
    if first is None:
        return None
    return first[0], first[1]


async def top_rarity_for_craft_cards(
    session: AsyncSession,
    cards: list[Card],
) -> RarityClass | None:
    """Highest rarity among trainer + material cards (by ``sort_order``)."""
    ids = {c.rarity_class_id for c in cards if c.rarity_class_id is not None}
    if not ids:
        return None
    rows = (
        await session.execute(select(RarityClass).where(RarityClass.id.in_(ids)))
    ).scalars().all()
    if not rows:
        return None
    return max(rows, key=lambda r: int(r.sort_order))


async def pick_craft_series(
    session: AsyncSession,
    top_rarity: RarityClass,
    *,
    rng: random.Random | None = None,
) -> CardSeries:
    """Pick an active pack series tier from the combined craft materials' rarity."""
    r = rng or random.Random()
    target_price = crystal_price_for_top_rarity(int(top_rarity.id))
    rows = list(
        (
            await session.execute(
                select(CardSeries).where(CardSeries.is_active.is_(True))
            )
        ).scalars()
    )
    if not rows:
        msg = "No active pack series — run pack series sync first."
        raise LookupError(msg)
    scored: list[tuple[int, CardSeries]] = []
    for series in rows:
        diff = abs(int(series.crystal_price) - target_price)
        scored.append((diff, series))
    scored.sort(key=lambda t: (t[0], t[1].crystal_price))
    best_diff = scored[0][0]
    pool = [s for d, s in scored if d <= best_diff + 2]
    if not pool:
        pool = [s for _, s in scored]
    return r.choice(pool)


async def run_craft(
    session: AsyncSession,
    *,
    discord_user_id: int,
    item_public_ids: list[str],
    trainer_public_id: str,
    guild_id: int | None = None,
) -> CraftSuccess | str:
    """Consume materials and mint one unopened pack. Returns error string on failure."""
    if len(item_public_ids) != CRAFT_ITEM_COUNT:
        return (
            f"You need exactly **{CRAFT_ITEM_COUNT}** item or Energy Card IDs "
            f"(got {len(item_public_ids)})."
        )

    unique_items = list(dict.fromkeys(item_public_ids))
    if len(unique_items) != CRAFT_ITEM_COUNT:
        return "Each item Card ID must be a different copy."

    if trainer_public_id.strip() in {p.strip() for p in unique_items}:
        return "The trainer Card ID cannot be one of the item copies."

    item_rows: list[tuple[UserCardInstance, Card]] = []
    for pid in unique_items:
        row = await _load_owned_row(session, discord_user_id=discord_user_id, public_id=pid)
        if row is None:
            return f"Item copy `{pid}` is not in your collection."
        inst, card = row
        blocked = await collection_sell_block_reason(
            session, discord_user_id=discord_user_id, instance_id=inst.id
        )
        if blocked:
            return blocked.replace("**", "")
        if not is_item_card(card):
            return f"`{pid}` is not an **Item** or **Energy** card."
        item_rows.append(row)

    trainer_row = await _load_owned_row(
        session, discord_user_id=discord_user_id, public_id=trainer_public_id
    )
    if trainer_row is None:
        return f"Trainer copy `{trainer_public_id}` is not in your collection."
    trainer_inst, trainer_card = trainer_row
    blocked_t = await collection_sell_block_reason(
        session, discord_user_id=discord_user_id, instance_id=trainer_inst.id
    )
    if blocked_t:
        return blocked_t.replace("**", "")
    if not is_craft_trainer_card(trainer_card):
        return (
            f"`{trainer_public_id}` is not a craftable **trainer** "
            "(Supporter / Stadium / Tool — not an Item card)."
        )

    uses = trainer_inst.craft_uses_remaining
    if uses is None:
        uses = CRAFT_TRAINER_MAX_USES
        trainer_inst.craft_uses_remaining = uses
    if int(uses) <= 0:
        return (
            f"**{trainer_card.name}** has no craft uses left "
            f"(0/{CRAFT_TRAINER_MAX_USES})."
        )

    craft_cards = [trainer_card] + [card for _, card in item_rows]
    top_rarity = await top_rarity_for_craft_cards(session, craft_cards)
    if top_rarity is None:
        return "Card rarity data is missing — try again after catalog sync."

    try:
        series = await pick_craft_series(session, top_rarity)
    except LookupError as exc:
        return str(exc)

    pack_service = PackService()
    pack = await pack_service._new_pack_instance(  # noqa: SLF001
        session,
        discord_user_id=discord_user_id,
        series_id=series.id,
        source="craft",
        guild_id=guild_id,
    )

    for inst, _ in item_rows:
        await session.delete(inst)

    trainer_inst.craft_uses_remaining = int(uses) - 1
    if trainer_inst.craft_uses_remaining <= 0:
        await session.delete(trainer_inst)
        trainer_remaining = 0
    else:
        trainer_remaining = int(trainer_inst.craft_uses_remaining)

    await session.flush()

    return CraftSuccess(
        pack=pack,
        series=series,
        trainer_name=trainer_card.name or "Trainer",
        trainer_uses_remaining=trainer_remaining if trainer_remaining > 0 else None,
        consumed_item_ids=[inst.public_id for inst, _ in item_rows],
        consumed_trainer_id=trainer_public_id,
        pack_tier_rarity=top_rarity.display_name or top_rarity.code or "Unknown",
    )


def serialize_craft_pack(pack: UserPackInstance, series: CardSeries) -> dict:
    return {
        "public_id": pack.public_id,
        "series": {
            "id": series.id,
            "code": series.code,
            "display_name": series.display_name,
            "crystal_price": int(series.crystal_price),
            "pack_art_url": series.pack_art_url,
        },
    }


async def open_crafted_pack_for_user(
    session: AsyncSession,
    *,
    discord_user_id: int,
    pack_public_id: str,
    guild_id: int | None = None,
) -> tuple[UserPackInstance, CardSeries] | str:
    """Open a crafted pack the user owns."""
    pid = normalize_public_id(pack_public_id)
    if pid is None:
        return "Invalid Pack ID."
    pack = await session.scalar(
        select(UserPackInstance).where(
            UserPackInstance.public_id == pid,
            UserPackInstance.discord_user_id == discord_user_id,
        )
    )
    if pack is None:
        return "That pack is not in your inventory."
    if pack.opened_at is not None:
        return "That pack was already opened."
    if (pack.source or "") != "craft":
        return "That pack was not created by crafting."

    ps = PackService()
    ds = DropService()
    try:
        opened = await ps.open_pack(
            session,
            ds,
            pack_instance_id=pack.id,
            owner_id=discord_user_id,
            guild_id=guild_id,
        )
    except Exception as exc:
        return str(exc)

    series = await session.get(CardSeries, opened.series_id)
    if series is None:
        return "Pack opened but series metadata is missing."
    return pack, series
