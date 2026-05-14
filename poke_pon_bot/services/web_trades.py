"""Interactive web trade sessions: invite, accept, update sides, ready/confirm, cancel, expire."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.auction import AUCTION_STATUS_ACTIVE, CardAuction
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.models.trade_session import (
    ACTIVE_TTL_MINUTES,
    INVITE_TTL_MINUTES,
    TRADE_STATUS_ACTIVE,
    TRADE_STATUS_CANCELLED,
    TRADE_STATUS_COMPLETED,
    TRADE_STATUS_DECLINED,
    TRADE_STATUS_EXPIRED,
    TRADE_STATUS_INVITED,
    TradeSession,
)
from poke_pon_bot.services.combat_deck import strip_instances_from_deck
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.trades import MAX_TRADE_CARDS_PER_SIDE, MAX_TRADE_CRYSTALS, MAX_TRADE_POKEDOLLARS
from poke_pon_bot.services.wallet import WalletService

_LIVE_STATUSES = (TRADE_STATUS_INVITED, TRADE_STATUS_ACTIVE)


def _to_int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _max_attack_damage(attacks: Any) -> int:
    """Best leading-digit ``damage`` field across a card's attacks (0 if none)."""
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_expired(ts: TradeSession) -> bool:
    exp = ts.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return _utc_now() >= exp.astimezone(UTC)


async def _user_has_live_session(session: AsyncSession, user_id: int) -> bool:
    row = await session.execute(
        select(TradeSession.id)
        .where(
            TradeSession.status.in_(_LIVE_STATUSES),
            or_(TradeSession.initiator_id == user_id, TradeSession.partner_id == user_id),
        )
        .limit(1),
    )
    return row.first() is not None


async def create_trade_invite(
    session: AsyncSession,
    *,
    initiator_id: int,
    partner_id: int,
) -> TradeSession | str:
    if initiator_id == partner_id:
        return "You cannot trade with yourself."
    if await _user_has_live_session(session, initiator_id):
        return "You already have an active trade session. Cancel it first."
    if await _user_has_live_session(session, partner_id):
        return "That user is already in a trade."
    ts = TradeSession(
        initiator_id=initiator_id,
        partner_id=partner_id,
        status=TRADE_STATUS_INVITED,
        initiator_card_ids=[],
        partner_card_ids=[],
        expires_at=_utc_now() + timedelta(minutes=INVITE_TTL_MINUTES),
    )
    session.add(ts)
    await session.flush()
    return ts


async def accept_trade_invite(
    session: AsyncSession,
    *,
    trade_id: int,
    user_id: int,
) -> str | None:
    ts = await session.get(TradeSession, trade_id)
    if ts is None or ts.status != TRADE_STATUS_INVITED:
        return "That trade invite no longer exists."
    if ts.partner_id != user_id:
        return "Only the invited user can accept."
    if _is_expired(ts):
        ts.status = TRADE_STATUS_EXPIRED
        return "This trade invite has expired."
    ts.status = TRADE_STATUS_ACTIVE
    ts.expires_at = _utc_now() + timedelta(minutes=ACTIVE_TTL_MINUTES)
    return None


async def decline_trade_invite(
    session: AsyncSession,
    *,
    trade_id: int,
    user_id: int,
) -> str | None:
    ts = await session.get(TradeSession, trade_id)
    if ts is None or ts.status != TRADE_STATUS_INVITED:
        return "That trade invite no longer exists."
    if ts.partner_id != user_id:
        return "Only the invited user can decline."
    ts.status = TRADE_STATUS_DECLINED
    return None


async def _validate_card_ids(
    session: AsyncSession,
    user_id: int,
    card_ids: list[int],
    trade_id: int,
) -> str | None:
    """Validate that the user owns all cards and none are in auctions or other trades."""
    if len(card_ids) > MAX_TRADE_CARDS_PER_SIDE:
        return f"Maximum {MAX_TRADE_CARDS_PER_SIDE} cards per side."
    if len(card_ids) != len(set(card_ids)):
        return "Duplicate cards in the list."
    for iid in card_ids:
        inst = await session.get(UserCardInstance, iid)
        if inst is None or inst.discord_user_id != user_id:
            return "You don't own one of the selected cards."
    if card_ids:
        auc_hit = await session.execute(
            select(CardAuction.id).where(
                CardAuction.instance_id.in_(card_ids),
                CardAuction.status == AUCTION_STATUS_ACTIVE,
            ).limit(1)
        )
        if auc_hit.first() is not None:
            return "One of the selected cards has an active auction."
        other_trades = await session.execute(
            select(TradeSession).where(
                TradeSession.status == TRADE_STATUS_ACTIVE,
                TradeSession.id != trade_id,
            )
        )
        card_set = set(card_ids)
        for ot in other_trades.scalars():
            other_ids = set(ot.initiator_card_ids or []) | set(ot.partner_card_ids or [])
            if card_set & other_ids:
                return "One of the selected cards is in another trade."
    return None


async def update_trade_side(
    session: AsyncSession,
    *,
    trade_id: int,
    user_id: int,
    card_ids: list[int],
    pokedollars: int,
    crystals: int,
) -> str | None:
    ts = await session.get(TradeSession, trade_id)
    if ts is None or ts.status != TRADE_STATUS_ACTIVE:
        return "This trade session is not active."
    if _is_expired(ts):
        ts.status = TRADE_STATUS_EXPIRED
        return "This trade session has expired."

    pd = max(0, min(int(pokedollars), MAX_TRADE_POKEDOLLARS))
    cr = max(0, min(int(crystals), MAX_TRADE_CRYSTALS))

    err = await _validate_card_ids(session, user_id, card_ids, trade_id)
    if err:
        return err

    if user_id == ts.initiator_id:
        ts.initiator_card_ids = card_ids
        ts.initiator_pokedollars = pd
        ts.initiator_crystals = cr
        ts.initiator_ready = False
    elif user_id == ts.partner_id:
        ts.partner_card_ids = card_ids
        ts.partner_pokedollars = pd
        ts.partner_crystals = cr
        ts.partner_ready = False
    else:
        return "You are not part of this trade."
    return None


async def toggle_ready(
    session: AsyncSession,
    wallet: WalletService,
    crystals_svc: CrystalsService,
    *,
    trade_id: int,
    user_id: int,
) -> tuple[str | None, bool]:
    """Toggle ready. Returns ``(error_or_none, trade_completed)``."""
    ts = await session.get(TradeSession, trade_id)
    if ts is None or ts.status != TRADE_STATUS_ACTIVE:
        return "This trade session is not active.", False
    if _is_expired(ts):
        ts.status = TRADE_STATUS_EXPIRED
        return "This trade session has expired.", False

    if user_id == ts.initiator_id:
        ts.initiator_ready = not ts.initiator_ready
    elif user_id == ts.partner_id:
        ts.partner_ready = not ts.partner_ready
    else:
        return "You are not part of this trade.", False

    if not (ts.initiator_ready and ts.partner_ready):
        return None, False

    err = await _execute_trade(session, wallet, crystals_svc, ts)
    if err:
        ts.initiator_ready = False
        ts.partner_ready = False
        return err, False
    return None, True


async def _execute_trade(
    session: AsyncSession,
    wallet: WalletService,
    crystals_svc: CrystalsService,
    ts: TradeSession,
) -> str | None:
    """Atomic swap — mirroring execute_trade_accept from trades.py with obtained_at refresh."""
    init_ids = list(ts.initiator_card_ids or [])
    part_ids = list(ts.partner_card_ids or [])

    if set(init_ids) & set(part_ids):
        return "Both sides reference the same card."

    init_pd = max(0, int(ts.initiator_pokedollars))
    init_cr = max(0, int(ts.initiator_crystals))
    part_pd = max(0, int(ts.partner_pokedollars))
    part_cr = max(0, int(ts.partner_crystals))

    has_anything = bool(init_ids or part_ids or init_pd or init_cr or part_pd or part_cr)
    if not has_anything:
        return "Nothing to trade."

    init_rows: list[UserCardInstance] = []
    for iid in init_ids:
        inst = await session.get(UserCardInstance, iid)
        if inst is None or inst.discord_user_id != ts.initiator_id:
            return "The initiator no longer owns a listed card."
        init_rows.append(inst)

    part_rows: list[UserCardInstance] = []
    for iid in part_ids:
        inst = await session.get(UserCardInstance, iid)
        if inst is None or inst.discord_user_id != ts.partner_id:
            return "The partner no longer owns a listed card."
        part_rows.append(inst)

    if init_pd:
        bal = await wallet.get_balance(session, ts.initiator_id)
        if bal < init_pd:
            return "The initiator doesn't have enough Pokedollars."
    if part_pd:
        bal = await wallet.get_balance(session, ts.partner_id)
        if bal < part_pd:
            return "The partner doesn't have enough Pokedollars."
    if init_cr:
        bal = await crystals_svc.get_balance(session, ts.initiator_id)
        if bal < init_cr:
            return "The initiator doesn't have enough Crystals."
    if part_cr:
        bal = await crystals_svc.get_balance(session, ts.partner_id)
        if bal < part_cr:
            return "The partner doesn't have enough Crystals."

    if init_pd:
        await wallet.try_debit(session, ts.initiator_id, init_pd)
    if part_pd:
        await wallet.try_debit(session, ts.partner_id, part_pd)
    if init_cr:
        await crystals_svc.try_debit(session, ts.initiator_id, init_cr)
    if part_cr:
        await crystals_svc.try_debit(session, ts.partner_id, part_cr)

    await strip_instances_from_deck(session, ts.initiator_id, set(init_ids))
    await strip_instances_from_deck(session, ts.partner_id, set(part_ids))

    now = _utc_now()
    for inst in init_rows:
        inst.discord_user_id = ts.partner_id
        inst.obtained_at = now
    for inst in part_rows:
        inst.discord_user_id = ts.initiator_id
        inst.obtained_at = now

    if init_pd:
        await wallet.try_credit(session, ts.partner_id, init_pd)
    if part_pd:
        await wallet.try_credit(session, ts.initiator_id, part_pd)
    if init_cr:
        await crystals_svc.try_credit(session, ts.partner_id, init_cr)
    if part_cr:
        await crystals_svc.try_credit(session, ts.initiator_id, part_cr)

    ts.status = TRADE_STATUS_COMPLETED
    return None


async def cancel_trade(
    session: AsyncSession,
    *,
    trade_id: int,
    user_id: int,
) -> str | None:
    ts = await session.get(TradeSession, trade_id)
    if ts is None or ts.status not in _LIVE_STATUSES:
        return "This trade session is not active."
    if user_id not in (ts.initiator_id, ts.partner_id):
        return "You are not part of this trade."
    ts.status = TRADE_STATUS_CANCELLED
    return None


async def expire_stale_sessions(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Mark expired invited/active sessions. Returns count."""
    count = 0
    async with async_session_factory() as session:
        rows = await session.execute(
            select(TradeSession).where(
                TradeSession.status.in_(_LIVE_STATUSES),
                TradeSession.expires_at <= _utc_now(),
            )
        )
        for ts in rows.scalars():
            ts.status = TRADE_STATUS_EXPIRED
            count += 1
        if count:
            await session.commit()
    return count


async def serialize_trade_session(
    session: AsyncSession,
    ts: TradeSession,
    *,
    viewer_id: int,
) -> dict:
    """Full state of a trade session for the API, including card details."""
    from poke_pon_bot.models.known_user import KnownUser

    def _utc_iso(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC).isoformat()
        return dt.astimezone(UTC).isoformat()

    async def _card_details(card_ids: list[int]) -> list[dict]:
        out = []
        for iid in (card_ids or []):
            inst = await session.get(UserCardInstance, iid)
            if inst is None:
                out.append({"instance_id": iid, "missing": True})
                continue
            card = await session.get(Card, inst.card_id)
            if card is None:
                out.append({"instance_id": iid, "public_id": inst.public_id, "missing": True})
                continue
            rarity: RarityClass | None = None
            if card.rarity_class_id is not None:
                rarity = await session.get(RarityClass, card.rarity_class_id)
            out.append({
                "instance_id": iid,
                "public_id": inst.public_id,
                "obtained_at": _utc_iso(inst.obtained_at),
                "missing": False,
                "card": {
                    "name": card.name,
                    "set_code": card.set_code,
                    "set_name": card.set_name,
                    "collector_number": card.collector_number,
                    "image_small_url": card.image_small_url,
                    "image_large_url": card.image_large_url,
                    "supertype": card.supertype,
                    "hp": _to_int_or_zero(card.hp),
                    "types": card.tcg_types or [],
                    "attacks": card.attacks or [],
                    "max_damage": _max_attack_damage(card.attacks),
                    "tcg_rarity": card.tcg_rarity,
                    "rarity": {
                        "code": rarity.code if rarity else None,
                        "display_name": rarity.display_name if rarity else None,
                        "sort_order": int(rarity.sort_order) if rarity else 0,
                    },
                },
            })
        return out

    async def _user_info(uid: int) -> dict:
        ku = await session.get(KnownUser, uid)
        if ku:
            return {"id": str(uid), "username": ku.username, "global_name": ku.global_name, "avatar_url": ku.avatar_url}
        return {"id": str(uid), "username": None, "global_name": None, "avatar_url": None}

    return {
        "id": ts.id,
        "status": ts.status,
        "initiator": await _user_info(ts.initiator_id),
        "partner": await _user_info(ts.partner_id),
        "initiator_side": {
            "cards": await _card_details(ts.initiator_card_ids),
            "pokedollars": int(ts.initiator_pokedollars),
            "crystals": int(ts.initiator_crystals),
            "ready": bool(ts.initiator_ready),
        },
        "partner_side": {
            "cards": await _card_details(ts.partner_card_ids),
            "pokedollars": int(ts.partner_pokedollars),
            "crystals": int(ts.partner_crystals),
            "ready": bool(ts.partner_ready),
        },
        "created_at": _utc_iso(ts.created_at),
        "expires_at": _utc_iso(ts.expires_at),
        "viewer_role": "initiator" if viewer_id == ts.initiator_id else "partner",
    }
