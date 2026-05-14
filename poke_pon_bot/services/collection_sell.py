"""Sell owned card copies to the shop for Pokedollars (collection view)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.auction import CardAuction
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.pending_trade import PendingTrade
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.combat_deck import strip_instances_from_deck
from poke_pon_bot.services.duel_engine import parse_hp
from poke_pon_bot.services.wallet import WalletService

# Printed tiers special_rare (8) and above need an explicit confirm step in Discord.
SELL_CONFIRM_MIN_SORT_ORDER = 8

# Reasonable bounds so chase pulls feel rewarding without replacing the economy.
_SELL_MIN_PAYOUT = 5
_SELL_MAX_PAYOUT = 6_000
COLLECTION_SELL_MIN_PAYOUT = _SELL_MIN_PAYOUT


def collection_sell_needs_confirm(rarity: RarityClass) -> bool:
    return int(rarity.sort_order) >= SELL_CONFIRM_MIN_SORT_ORDER


def quote_collection_sell_payout(card: Card, rarity: RarityClass, inst: UserCardInstance) -> int:
    """Balanced payout: tier drives the floor; HP / attacks / evolution add more on high rarities."""
    tier = max(1, min(10, int(rarity.sort_order)))
    # ~35 @ common → ~170 @ chase (was ~20–110).
    base = 20 + tier * 15

    hp = parse_hp(card.hp)
    hp_part = min(max(hp, 0), 400) // 8

    attacks = card.attacks if isinstance(card.attacks, list) else []
    atk_part = min(len(attacks), 12) * 3

    evo = max(0, int(inst.evolution_stages or 0))
    evo_part = min(evo * 14, 90)

    stat_sum = hp_part + atk_part + evo_part
    # Stat upsides matter a bit more on expensive printings (1.0× … ~1.45×).
    tier_stat_mult = 1.0 + 0.05 * (tier - 1)
    stats_valued = int(stat_sum * tier_stat_mult)

    total = base + stats_valued
    return max(_SELL_MIN_PAYOUT, min(int(total), _SELL_MAX_PAYOUT))


_AUCT_BLOCK = (
    "That copy is in an **active auction**. End the listing or wait for it to finish first."
)
_TRADE_BLOCK = (
    "That copy is tied up in a **pending trade**. Finish or cancel the trade first."
)


async def collection_sell_block_reasons_for_instances(
    session: AsyncSession,
    *,
    discord_user_id: int,
    instance_ids: Iterable[int],
) -> dict[int, str | None]:
    """Map each requested ``instance_id`` to a block reason, or ``None`` if OK to sell."""
    ids = {int(i) for i in instance_ids}
    if not ids:
        return {}
    out: dict[int, str | None] = {iid: None for iid in ids}

    auction_hits = (
        await session.execute(
            select(CardAuction.instance_id).where(
                CardAuction.instance_id.in_(ids),
                CardAuction.status == "active",
            )
        )
    ).scalars()
    for aid in auction_hits:
        out[int(aid)] = _AUCT_BLOCK

    trade_blocked: set[int] = set()
    tr_rows = await session.execute(
        select(PendingTrade).where(
            or_(
                PendingTrade.initiator_id == discord_user_id,
                PendingTrade.partner_id == discord_user_id,
            ),
        )
    )
    for pt in tr_rows.scalars():
        give = set(pt.give_instance_ids or [])
        recv = set(pt.receive_instance_ids or [])
        trade_blocked.update((give | recv) & ids)

    for iid in trade_blocked:
        if out.get(iid) is None:
            out[iid] = _TRADE_BLOCK

    return out


async def collection_sell_block_reason(
    session: AsyncSession,
    *,
    discord_user_id: int,
    instance_id: int,
) -> str | None:
    """Return a user-facing reason this copy can't be sold, or ``None`` if OK."""
    m = await collection_sell_block_reasons_for_instances(
        session,
        discord_user_id=discord_user_id,
        instance_ids=[instance_id],
    )
    return m.get(instance_id)


@dataclass(frozen=True)
class CollectionSellOutcome:
    ok: bool
    error: str | None = None
    payout: int = 0
    new_balance: int = 0
    card_name: str = ""


async def run_collection_sell(
    session: AsyncSession,
    wallet: WalletService,
    *,
    discord_user_id: int,
    instance_id: int,
    expected_payout: int | None = None,
) -> CollectionSellOutcome:
    """Delete the instance, credit ``quote``, commit. Caller should open the session transaction."""
    inst = await session.get(UserCardInstance, instance_id)
    if inst is None or inst.discord_user_id != discord_user_id:
        return CollectionSellOutcome(ok=False, error="That card isn’t in your collection anymore.")
    card = await session.get(Card, inst.card_id)
    if card is None:
        return CollectionSellOutcome(ok=False, error="Catalog data for that card is missing.")
    rarity = await session.get(RarityClass, card.rarity_class_id)
    if rarity is None:
        return CollectionSellOutcome(ok=False, error="Rarity data missing — try again after a sync.")

    blocked = await collection_sell_block_reason(session, discord_user_id=discord_user_id, instance_id=instance_id)
    if blocked is not None:
        return CollectionSellOutcome(ok=False, error=blocked)

    payout = quote_collection_sell_payout(card, rarity, inst)
    if expected_payout is not None and payout != expected_payout:
        return CollectionSellOutcome(
            ok=False,
            error="Sell quote changed — open your collection again and retry.",
        )

    await strip_instances_from_deck(session, discord_user_id, {instance_id})
    await session.delete(inst)
    new_bal = await wallet.try_credit(session, discord_user_id, payout)
    await session.commit()
    return CollectionSellOutcome(
        ok=True,
        payout=payout,
        new_balance=new_bal,
        card_name=card.name,
    )
