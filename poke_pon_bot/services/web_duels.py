"""Interactive web duel sessions: invite, accept, state, cancel, expire, settle."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.duel_session import (
    ABANDON_CANCEL_MINUTES,
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


def _parse_iso_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            s = str(raw).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def mark_duel_room_occupied(row: DuelSession) -> None:
    """Clear abandonment timer when someone reconnects to the duel room."""
    st = dict(row.state or {})
    if "room_empty_since" not in st:
        return
    st.pop("room_empty_since", None)
    row.state = st


def mark_duel_room_empty(row: DuelSession) -> None:
    """Record when the duel WS room last had zero connections."""
    st = dict(row.state or {})
    st["room_empty_since"] = _utc_now().isoformat()
    row.state = st


def _should_cancel_abandoned(row: DuelSession, *, now: datetime | None = None) -> bool:
    if row.status not in _LIVE_STATUSES:
        return False
    empty_since = _parse_iso_datetime((row.state or {}).get("room_empty_since"))
    if empty_since is None:
        return False
    now = now or _utc_now()
    return now - empty_since >= timedelta(minutes=ABANDON_CANCEL_MINUTES)


def _duel_has_live_websocket(duel_id: int) -> bool:
    try:
        from poke_pon_bot.web.duel_ws import duel_has_websocket_clients

        return duel_has_websocket_clients(duel_id)
    except ImportError:
        return False


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


async def expire_stale_duels(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[int]:
    """Expire TTL'd duels and cancel abandoned ones. Returns duel ids that changed."""
    now = _utc_now()
    notify_ids: list[int] = []
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(DuelSession).where(DuelSession.status.in_(_LIVE_STATUSES))
            )
        ).scalars().all()
        changed = False
        for row in rows:
            did = int(row.id)
            if _is_expired(row):
                row.status = DUEL_STATUS_EXPIRED
                if row.escrow_locked_at and not row.escrow_paid_out_at:
                    await refund_escrow(db, row)
                notify_ids.append(did)
                changed = True
                continue
            if not _should_cancel_abandoned(row, now=now):
                continue
            if _duel_has_live_websocket(did):
                mark_duel_room_occupied(row)
                changed = True
                continue
            row.status = DUEL_STATUS_CANCELLED
            st = dict(row.state or {})
            log = list(st.get("log") or [])
            log.append({"type": "abandoned", "at": now.isoformat()})
            st["log"] = log
            st.pop("room_empty_since", None)
            row.state = st
            if row.escrow_locked_at and not row.escrow_paid_out_at:
                await refund_escrow(db, row)
            notify_ids.append(did)
            changed = True
        if changed:
            await db.commit()
    return notify_ids


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


async def validate_duel_accept_prerequisites(
    session: AsyncSession,
    *,
    row: DuelSession,
    wallet: WalletService,
    crystals: CrystalsService,
) -> str | None:
    """Deck + stake checks before flipping invite → active."""
    init_ids = await get_saved_instance_ids(session, row.initiator_id)
    part_ids = await get_saved_instance_ids(session, row.partner_id)
    if not init_ids:
        return (
            "The initiator has no saved duel deck. "
            "They must save a deck on the website Deck editor first."
        )
    if not part_ids:
        return (
            "You need a saved duel deck before accepting. "
            "Open Deck editor, save 1–6 Pokémon, then try again."
        )

    amt = int(row.bet_amount or 0)
    if amt <= 0:
        return None
    cur = normalize_duel_currency(row.bet_currency)
    if cur == DUEL_BID_CURRENCY_CRYSTALS:
        for label, uid in (("Initiator", row.initiator_id), ("You", row.partner_id)):
            bal = await crystals.get_balance(session, uid)
            if bal < amt:
                return (
                    f"{label} does not have enough crystals for the stake "
                    f"({bal:,} / {amt:,} needed)."
                )
    else:
        for label, uid in (("Initiator", row.initiator_id), ("You", row.partner_id)):
            bal = await wallet.get_balance(session, uid)
            if bal < amt:
                return (
                    f"{label} does not have enough Pokedollars for the stake "
                    f"({bal:,} / {amt:,} needed)."
                )
    return None


async def accept_duel_invite(session: AsyncSession, *, duel_id: int, user_id: int) -> str | None:
    row = await session.get(DuelSession, duel_id)
    if row is None:
        return "That duel invite no longer exists."
    if row.partner_id != user_id:
        return "Only the invited user can accept."
    if row.status == DUEL_STATUS_ACTIVE:
        return None
    if row.status != DUEL_STATUS_INVITED:
        return "That duel invite no longer exists."
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
    st = dict(row.state or {})
    st["winner"] = int(winner)
    log = list(st.get("log") or [])
    log.append(
        {
            "type": "surrender",
            "actor": int(user_id),
            "winner": int(winner),
        }
    )
    st["log"] = log
    row.state = st
    row.version = int(row.version or 0) + 1
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

