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


@dataclass(frozen=True)
class CollectionBulkSellOutcome:
    ok: bool
    error: str | None = None
    payout: int = 0
    new_balance: int = 0
    sold_count: int = 0
    blocked_instance_ids: tuple[int, ...] = ()
    needs_confirm: bool = False


async def quote_collection_bulk_sell(
    session: AsyncSession,
    *,
    discord_user_id: int,
    instance_ids: Iterable[int],
) -> tuple[int, bool, dict[int, int], dict[int, str | None]]:
    """Return (total_quote, needs_confirm, per_instance_quote, block_map).

    - Missing/invalid/unauthorized instances are treated as blocked with a reason.
    - Instances in active auctions or pending trades are blocked via the shared rules.
    """
    ids = [int(i) for i in instance_ids]
    uniq: list[int] = list(dict.fromkeys(ids))
    if not uniq:
        return 0, False, {}, {}

    block_map = await collection_sell_block_reasons_for_instances(
        session,
        discord_user_id=discord_user_id,
        instance_ids=uniq,
    )
    # Mark "not owned / missing" as blocked too (the block fn only handles auction/trade).
    owned_rows = (
        await session.execute(
            select(UserCardInstance.id, UserCardInstance.card_id)
            .where(
                UserCardInstance.id.in_(uniq),
                UserCardInstance.discord_user_id == discord_user_id,
            )
        )
    ).all()
    owned_card_by_inst: dict[int, int] = {int(iid): int(cid) for iid, cid in owned_rows}
    for iid in uniq:
        if iid not in owned_card_by_inst and block_map.get(iid) is None:
            block_map[iid] = "That card isn’t in your collection anymore."

    card_ids = sorted(set(owned_card_by_inst.values()))
    cards_by_id: dict[int, Card] = {}
    rarities_by_id: dict[int, RarityClass] = {}
    if card_ids:
        c_rows = (
            await session.execute(select(Card).where(Card.id.in_(card_ids)))
        ).scalars()
        cards_by_id = {int(c.id): c for c in c_rows if c.id is not None}
        r_rows = (
            await session.execute(
                select(RarityClass).where(RarityClass.id.in_([c.rarity_class_id for c in cards_by_id.values()]))
            )
        ).scalars()
        rarities_by_id = {int(r.id): r for r in r_rows if r.id is not None}

    per_quote: dict[int, int] = {}
    needs_confirm = False
    total = 0
    for iid in uniq:
        if block_map.get(iid) is not None:
            continue
        card_id = owned_card_by_inst.get(iid)
        if card_id is None:
            continue
        card = cards_by_id.get(card_id)
        if card is None:
            block_map[iid] = "Catalog data for that card is missing."
            continue
        rarity = rarities_by_id.get(int(card.rarity_class_id))
        if rarity is None:
            block_map[iid] = "Rarity data missing — try again after a sync."
            continue
        if collection_sell_needs_confirm(rarity):
            needs_confirm = True
        # Pull inst for evo stages / ownership-checked quote.
        inst = await session.get(UserCardInstance, iid)
        if inst is None or inst.discord_user_id != discord_user_id:
            block_map[iid] = "That card isn’t in your collection anymore."
            continue
        q = quote_collection_sell_payout(card, rarity, inst)
        per_quote[iid] = q
        total += q

    return total, needs_confirm, per_quote, block_map


async def run_collection_bulk_sell(
    session: AsyncSession,
    wallet: WalletService,
    *,
    discord_user_id: int,
    instance_ids: Iterable[int],
    expected_payout: int | None = None,
) -> CollectionBulkSellOutcome:
    """Sell many owned instances in one atomic transaction.

    This function:
    - validates ownership and block rules for every instance
    - computes the total quote
    - strips sold instances from the user's duel deck
    - deletes the instances
    - credits the wallet once
    - commits once
    """
    ids = [int(i) for i in instance_ids]
    uniq: list[int] = list(dict.fromkeys(ids))
    if not uniq:
        return CollectionBulkSellOutcome(ok=False, error="Select at least one card to sell.")

    total, needs_confirm, per_quote, block_map = await quote_collection_bulk_sell(
        session,
        discord_user_id=discord_user_id,
        instance_ids=uniq,
    )
    blocked_ids = tuple(sorted(iid for iid, reason in block_map.items() if reason is not None))
    if blocked_ids:
        return CollectionBulkSellOutcome(
            ok=False,
            error="Some selected cards cannot be sold.",
            blocked_instance_ids=blocked_ids,
            needs_confirm=needs_confirm,
        )
    if expected_payout is not None and int(expected_payout) != int(total):
        return CollectionBulkSellOutcome(
            ok=False,
            error="Sell quote changed — refresh and retry.",
            payout=total,
            needs_confirm=needs_confirm,
        )

    # Re-check instances exist right before delete (covers TOCTOU within one session).
    inst_rows = (
        await session.execute(
            select(UserCardInstance.id)
            .where(
                UserCardInstance.id.in_(uniq),
                UserCardInstance.discord_user_id == discord_user_id,
            )
        )
    ).scalars().all()
    inst_ids = {int(x) for x in inst_rows}
    if len(inst_ids) != len(uniq):
        return CollectionBulkSellOutcome(
            ok=False,
            error="That selection changed — refresh and retry.",
            needs_confirm=needs_confirm,
        )

    await strip_instances_from_deck(session, discord_user_id, inst_ids)
    for iid in inst_ids:
        inst = await session.get(UserCardInstance, iid)
        if inst is not None:
            await session.delete(inst)

    new_bal = await wallet.try_credit(session, discord_user_id, int(total))
    await session.commit()
    return CollectionBulkSellOutcome(
        ok=True,
        payout=int(total),
        new_balance=int(new_bal),
        sold_count=len(inst_ids),
        needs_confirm=needs_confirm,
    )
