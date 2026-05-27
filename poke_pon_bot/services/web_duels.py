"""Interactive web duel sessions: invite, accept, state, cancel, expire, settle."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.duel_session import (
    ACTIVE_TTL_MINUTES,
    DUEL_BID_CURRENCY_CRYSTALS,
    DUEL_BID_CURRENCY_POKEDOLLARS,
    DUEL_STATUS_ACTIVE,
    DUEL_STATUS_CANCELLED,
    DUEL_STATUS_COMPLETED,
    DUEL_STATUS_DECLINED,
    DUEL_STATUS_EXPIRED,
    DUEL_STATUS_INVITED,
    INVITE_TTL_MINUTES,
    DuelSession,
)
from poke_pon_bot.services.combat_deck import load_fighters_ordered, get_saved_instance_ids
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.duel_engine import Fighter
from poke_pon_bot.services.wallet import WalletService


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_expired(row: DuelSession) -> bool:
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return _utc_now() >= exp.astimezone(UTC)


_LIVE_STATUSES = (DUEL_STATUS_INVITED, DUEL_STATUS_ACTIVE)


async def _user_has_live_duel(session: AsyncSession, user_id: int) -> bool:
    hit = await session.execute(
        select(DuelSession.id)
        .where(
            DuelSession.status.in_(_LIVE_STATUSES),
            or_(DuelSession.initiator_id == user_id, DuelSession.partner_id == user_id),
        )
        .limit(1)
    )
    return hit.first() is not None


def normalize_duel_currency(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in ("cr", "crystals", "crystal"):
        return DUEL_BID_CURRENCY_CRYSTALS
    return DUEL_BID_CURRENCY_POKEDOLLARS


async def expire_stale_duels(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(DuelSession).where(DuelSession.status.in_(_LIVE_STATUSES))
            )
        ).scalars().all()
        changed = False
        for row in rows:
            if not _is_expired(row):
                continue
            row.status = DUEL_STATUS_EXPIRED
            # refund if escrow was locked but duel never finished
            if row.escrow_locked_at and not row.escrow_paid_out_at:
                await refund_escrow(db, row)
            changed = True
        if changed:
            await db.commit()


async def create_duel_invite(
    session: AsyncSession,
    *,
    initiator_id: int,
    partner_id: int,
    bet_currency: str,
    bet_amount: int,
) -> DuelSession | str:
    if initiator_id == partner_id:
        return "You cannot duel yourself."
    if await _user_has_live_duel(session, initiator_id):
        return "You already have an active duel session. Cancel it first."
    if await _user_has_live_duel(session, partner_id):
        return "That user is already in a duel."

    cur = normalize_duel_currency(bet_currency)
    amt = max(0, int(bet_amount or 0))
    row = DuelSession(
        initiator_id=initiator_id,
        partner_id=partner_id,
        status=DUEL_STATUS_INVITED,
        bet_currency=cur,
        bet_amount=amt,
        state={},
        version=0,
        expires_at=_utc_now() + timedelta(minutes=INVITE_TTL_MINUTES),
    )
    session.add(row)
    await session.flush()
    return row


async def accept_duel_invite(session: AsyncSession, *, duel_id: int, user_id: int) -> str | None:
    row = await session.get(DuelSession, duel_id)
    if row is None or row.status != DUEL_STATUS_INVITED:
        return "That duel invite no longer exists."
    if row.partner_id != user_id:
        return "Only the invited user can accept."
    if _is_expired(row):
        row.status = DUEL_STATUS_EXPIRED
        return "This duel invite has expired."

    row.status = DUEL_STATUS_ACTIVE
    row.expires_at = _utc_now() + timedelta(minutes=ACTIVE_TTL_MINUTES)
    return None


async def decline_duel_invite(session: AsyncSession, *, duel_id: int, user_id: int) -> str | None:
    row = await session.get(DuelSession, duel_id)
    if row is None or row.status != DUEL_STATUS_INVITED:
        return "That duel invite no longer exists."
    if row.partner_id != user_id:
        return "Only the invited user can decline."
    row.status = DUEL_STATUS_DECLINED
    return None


async def cancel_duel(session: AsyncSession, *, duel_id: int, user_id: int) -> str | None:
    row = await session.get(DuelSession, duel_id)
    if row is None or row.status not in _LIVE_STATUSES:
        return "That duel session is not active."
    if user_id not in (row.initiator_id, row.partner_id):
        return "You are not part of this duel."
    if _is_expired(row):
        row.status = DUEL_STATUS_EXPIRED
        if row.escrow_locked_at and not row.escrow_paid_out_at:
            await refund_escrow(session, row)
        return "This duel session has expired."
    row.status = DUEL_STATUS_CANCELLED
    if row.escrow_locked_at and not row.escrow_paid_out_at:
        await refund_escrow(session, row)
    return None


async def surrender_duel(session: AsyncSession, *, duel_id: int, user_id: int) -> str | None:
    row = await session.get(DuelSession, duel_id)
    if row is None or row.status != DUEL_STATUS_ACTIVE:
        return "This duel session is not active."
    if user_id not in (row.initiator_id, row.partner_id):
        return "You are not part of this duel."
    if _is_expired(row):
        row.status = DUEL_STATUS_EXPIRED
        if row.escrow_locked_at and not row.escrow_paid_out_at:
            await refund_escrow(session, row)
        return "This duel session has expired."

    winner = row.partner_id if user_id == row.initiator_id else row.initiator_id
    row.status = DUEL_STATUS_COMPLETED
    row.winner_id = winner
    return None


async def lock_escrow(
    session: AsyncSession,
    *,
    row: DuelSession,
    wallet: WalletService,
    crystals: CrystalsService,
) -> str | None:
    if row.bet_amount <= 0:
        # no bet
        row.escrow_currency = None
        row.escrow_amount_each = 0
        row.escrow_locked_at = _utc_now()
        return None
    if row.escrow_locked_at:
        return None

    amt = int(row.bet_amount)
    cur = normalize_duel_currency(row.bet_currency)

    # Debit both upfront (simple MVP escrow: record on duel_sessions row).
    if cur == DUEL_BID_CURRENCY_CRYSTALS:
        ok_a = await crystals.try_debit(session, row.initiator_id, amt)
        ok_b = await crystals.try_debit(session, row.partner_id, amt)
        if not ok_a or not ok_b:
            # best-effort rollback if one succeeded
            if ok_a:
                await crystals.try_credit(session, row.initiator_id, amt)
            if ok_b:
                await crystals.try_credit(session, row.partner_id, amt)
            return "One of the players does not have enough crystals to cover the stake."
    else:
        ok_a = await wallet.try_debit(session, row.initiator_id, amt)
        ok_b = await wallet.try_debit(session, row.partner_id, amt)
        if not ok_a or not ok_b:
            if ok_a:
                await wallet.try_credit(session, row.initiator_id, amt)
            if ok_b:
                await wallet.try_credit(session, row.partner_id, amt)
            return "One of the players does not have enough pokedollars to cover the stake."

    row.escrow_currency = cur
    row.escrow_amount_each = amt
    row.escrow_locked_at = _utc_now()
    return None


async def refund_escrow(session: AsyncSession, row: DuelSession) -> None:
    if not row.escrow_locked_at or row.escrow_paid_out_at:
        return
    cur = normalize_duel_currency(row.escrow_currency)
    amt = int(row.escrow_amount_each or 0)
    if amt <= 0:
        row.escrow_paid_out_at = _utc_now()
        return
    wallet = WalletService()
    crystals = CrystalsService()
    if cur == DUEL_BID_CURRENCY_CRYSTALS:
        await crystals.try_credit(session, row.initiator_id, amt)
        await crystals.try_credit(session, row.partner_id, amt)
    else:
        await wallet.try_credit(session, row.initiator_id, amt)
        await wallet.try_credit(session, row.partner_id, amt)
    row.escrow_paid_out_at = _utc_now()


async def payout_escrow(session: AsyncSession, row: DuelSession, *, winner_id: int) -> None:
    if not row.escrow_locked_at or row.escrow_paid_out_at:
        row.escrow_paid_out_at = _utc_now()
        return
    cur = normalize_duel_currency(row.escrow_currency)
    amt = int(row.escrow_amount_each or 0)
    total = amt * 2
    wallet = WalletService()
    crystals = CrystalsService()
    if total > 0:
        if cur == DUEL_BID_CURRENCY_CRYSTALS:
            await crystals.try_credit(session, winner_id, total)
        else:
            await wallet.try_credit(session, winner_id, total)
    row.escrow_paid_out_at = _utc_now()


async def build_initial_state(
    session: AsyncSession,
    *,
    duel_id: int,
    initiator_id: int,
    partner_id: int,
) -> dict[str, Any] | str:
    """Load both saved decks (1–6) and produce an initial state skeleton."""
    init_ids = await get_saved_instance_ids(session, initiator_id)
    part_ids = await get_saved_instance_ids(session, partner_id)
    if not init_ids:
        return "The initiator has no saved duel deck."
    if not part_ids:
        return "The invited player has no saved duel deck."

    init_pairs = await load_fighters_ordered(session, initiator_id, init_ids)
    if isinstance(init_pairs, str):
        return init_pairs
    part_pairs = await load_fighters_ordered(session, partner_id, part_ids)
    if isinstance(part_pairs, str):
        return part_pairs

    # Minimal state; advanced engine will enrich.
    rng = random.Random(int(duel_id))
    first = initiator_id if rng.random() < 0.5 else partner_id

    def _serialize_fighter(inst, card) -> dict[str, Any]:
        f = Fighter.from_instance(inst, card)
        return {
            "instance_id": int(f.instance_id),
            "public_id": f.public_id,
            "name": f.name,
            "image_small": f.image_small,
            "image_large": f.image_large,
            "max_hp": int(f.max_hp),
            "current_hp": int(f.current_hp),
            "types": list(f.types),
            "attacks": f.attacks,
        }

    return {
        "duel_id": int(duel_id),
        "players": [int(initiator_id), int(partner_id)],
        "turn": int(first),
        "draws_remaining": 2,
        "lineups": {
            str(initiator_id): [_serialize_fighter(inst, card) for inst, card in init_pairs],
            str(partner_id): [_serialize_fighter(inst, card) for inst, card in part_pairs],
        },
        "energy_pool": {str(initiator_id): {}, str(partner_id): {}},
        "items": {str(initiator_id): [], str(partner_id): []},
        "buffs": {},
        "shields": {},
        "log": [],
        "winner": None,
    }

