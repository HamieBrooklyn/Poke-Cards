"""Player-to-player trades (inventory instances + Pokedollars)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.pending_trade import PendingTrade
from poke_pon_bot.services.combat_deck import strip_instances_from_deck
from poke_pon_bot.services.instance_public_id import normalize_public_id
from poke_pon_bot.services.wallet import WalletService

MAX_TRADE_CARDS_PER_SIDE = 10
MAX_TRADE_POKEDOLLARS = 9_999_999
MAX_TRADE_CRYSTALS = 9_999_999
TRADE_OFFER_TTL_MINUTES = 15


def split_card_tokens(raw: str | None) -> list[str]:
    if raw is None or not str(raw).strip():
        return []
    return [p.strip() for p in re.split(r"[\s,]+", str(raw).strip()) if p.strip()]


async def resolve_owned_instances(
    session: AsyncSession,
    discord_user_id: int,
    tokens: list[str],
) -> tuple[list[UserCardInstance], str | None]:
    """Resolve Card IDs (public ids) to instances owned by ``discord_user_id``."""
    if len(tokens) > MAX_TRADE_CARDS_PER_SIDE:
        return [], f"You can trade at most **{MAX_TRADE_CARDS_PER_SIDE}** cards per side."
    seen_norm: set[str] = set()
    resolved_ids: list[int] = []
    for tok in tokens:
        norm = normalize_public_id(tok)
        if norm is None:
            return [], f"Invalid Card ID: `{tok}`."
        if norm in seen_norm:
            return [], "You listed the same Card ID twice."
        seen_norm.add(norm)
        row = await session.execute(
            select(UserCardInstance.id).where(
                UserCardInstance.discord_user_id == discord_user_id,
                UserCardInstance.public_id == norm,
            ),
        )
        first = row.first()
        if first is None:
            return [], f"You don’t own a card with Card ID `{tok}`."
        resolved_ids.append(int(first[0]))
    instances: list[UserCardInstance] = []
    for iid in resolved_ids:
        inst = await session.get(UserCardInstance, iid)
        if inst is None or inst.discord_user_id != discord_user_id:
            return [], "A listed card is no longer in your collection."
        instances.append(inst)
    return instances, None


async def delete_pending_as_initiator(session: AsyncSession, initiator_id: int) -> None:
    """Remove open offers created by this user (superseded by a new proposal)."""
    await session.execute(delete(PendingTrade).where(PendingTrade.initiator_id == initiator_id))


def trade_is_expired(pt: PendingTrade) -> bool:
    exp = pt.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return datetime.now(UTC) >= exp.astimezone(UTC)


async def execute_trade_accept(
    session: AsyncSession,
    wallet: WalletService,
    *,
    pending_id: int,
    accepting_user_id: int,
) -> str | None:
    """Partner accepts — atomic swap. Returns error message or ``None``."""
    pt = await session.get(PendingTrade, pending_id)
    if pt is None:
        return "That trade offer no longer exists."
    if accepting_user_id != pt.partner_id:
        return "Only the invited player can accept."
    if trade_is_expired(pt):
        await session.delete(pt)
        return "This trade offer **expired**. Ask for a new one."

    give_ids = list(pt.give_instance_ids or [])
    recv_ids = list(pt.receive_instance_ids or [])
    if len(give_ids) != len(set(give_ids)) or len(recv_ids) != len(set(recv_ids)):
        await session.delete(pt)
        return "Trade data was invalid — offer discarded."

    overlap = set(give_ids) & set(recv_ids)
    if overlap:
        await session.delete(pt)
        return "Trade referenced the same instance twice — offer discarded."

    give_money = max(0, int(pt.give_pokedollars))
    recv_money = max(0, int(pt.receive_pokedollars))
    if give_money > MAX_TRADE_POKEDOLLARS or recv_money > MAX_TRADE_POKEDOLLARS:
        await session.delete(pt)
        return "Trade amounts were out of range — offer discarded."

    initiator_has = bool(give_ids or give_money > 0)
    partner_has = bool(recv_ids or recv_money > 0)
    if not initiator_has and not partner_has:
        await session.delete(pt)
        return "There was nothing to trade."

    give_rows: list[UserCardInstance] = []
    for iid in give_ids:
        inst = await session.get(UserCardInstance, iid)
        if inst is None or inst.discord_user_id != pt.initiator_id:
            return "The initiator no longer has every listed card."
        give_rows.append(inst)

    recv_rows: list[UserCardInstance] = []
    for iid in recv_ids:
        inst = await session.get(UserCardInstance, iid)
        if inst is None or inst.discord_user_id != pt.partner_id:
            return "You no longer have every listed card on your side."
        recv_rows.append(inst)

    if give_money:
        bi = await wallet.get_balance(session, pt.initiator_id)
        if bi < give_money:
            return "The initiator doesn’t have enough **Pokedollars** anymore."
    if recv_money:
        bp = await wallet.get_balance(session, pt.partner_id)
        if bp < recv_money:
            return "You don’t have enough **Pokedollars** for your side anymore."

    await wallet.try_debit(session, pt.initiator_id, give_money)
    await wallet.try_debit(session, pt.partner_id, recv_money)

    strip_instances_from_deck(session, pt.initiator_id, set(give_ids))
    strip_instances_from_deck(session, pt.partner_id, set(recv_ids))

    for inst in give_rows:
        inst.discord_user_id = pt.partner_id
    for inst in recv_rows:
        inst.discord_user_id = pt.initiator_id

    if give_money:
        await wallet.try_credit(session, pt.partner_id, give_money)
    if recv_money:
        await wallet.try_credit(session, pt.initiator_id, recv_money)

    await session.delete(pt)
    return None


async def decline_trade(session: AsyncSession, *, pending_id: int, decliner_id: int) -> str | None:
    pt = await session.get(PendingTrade, pending_id)
    if pt is None:
        return "That trade offer no longer exists."
    if decliner_id != pt.partner_id:
        return "Only the invited player can decline."
    await session.delete(pt)
    return None


async def cancel_trade(session: AsyncSession, *, pending_id: int, canceller_id: int) -> str | None:
    pt = await session.get(PendingTrade, pending_id)
    if pt is None:
        return "That trade offer no longer exists."
    if canceller_id != pt.initiator_id:
        return "Only whoever opened the trade can cancel it."
    await session.delete(pt)
    return None
