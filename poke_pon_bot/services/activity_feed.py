"""Aggregate recent user events for the unified website activity feed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from poke_pon_bot.models.auction import AUCTION_STATUS_ENDED_SOLD, CardAuction
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.mission import UserMission
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.models.trade_session import TRADE_STATUS_COMPLETED, TradeSession
from poke_pon_bot.services.missions import MissionService
from poke_pon_bot.services.rarity_luck_boost import (
    SCHEDULED_LUCK_UPDATED_BY,
    get_luck_boost_row,
    resolve_active_rarity_luck_boost,
)
from poke_pon_bot.models.scheduled_game_event import EVENT_KIND_LUCK_BOOST
from poke_pon_bot.services.event_scheduler import resolve_active_effects

# Sources shown as card-acquisition events (excludes dev and auction transfers).
_ACQUIRE_SOURCES = frozenset(
    {
        "drop",
        "pack_open",
        "pack_code_card",
        "purchase_pokedollars",
        "purchase_crystals",
        "purchase_crystals_random",
        "sku_consumable",
        "tutorial",
        "craft",
        "assembly",
    }
)

_SOURCE_LABELS: dict[str, str] = {
    "drop": "Card drop",
    "pack_open": "Pack opened",
    "pack_code_card": "Pack reward",
    "purchase_pokedollars": "Shop purchase",
    "purchase_crystals": "Crystal pack",
    "purchase_crystals_random": "Random crystal pack",
    "sku_consumable": "Shop bundle",
    "tutorial": "Tutorial reward",
    "craft": "Crafted",
    "assembly": "Assembled",
}

_PER_SOURCE_LIMIT = 30
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class _FeedRow:
    kind: str
    at: datetime
    payload: dict[str, Any]


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).isoformat()
    return dt.astimezone(UTC).isoformat()


def _card_brief(card: Card, rarity: RarityClass | None) -> dict[str, Any]:
    return {
        "name": card.name,
        "set_code": card.set_code,
        "set_name": card.set_name,
        "image_small_url": card.image_small_url,
        "rarity": {
            "code": rarity.code if rarity else None,
            "display_name": rarity.display_name if rarity else None,
        },
    }


async def _fetch_acquire_events(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int,
) -> list[_FeedRow]:
    stmt = (
        select(UserCardInstance, Card, RarityClass)
        .join(Card, UserCardInstance.card_id == Card.id)
        .outerjoin(RarityClass, Card.rarity_class_id == RarityClass.id)
        .where(
            UserCardInstance.discord_user_id == user_id,
            UserCardInstance.source.in_(_ACQUIRE_SOURCES),
            UserCardInstance.auction_obtained_at.is_(None),
        )
        .order_by(desc(UserCardInstance.obtained_at))
        .limit(limit)
    )
    rows = await session.execute(stmt)
    out: list[_FeedRow] = []
    for inst, card, rarity in rows.all():
        at = inst.obtained_at
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        out.append(
            _FeedRow(
                kind="drop",
                at=at,
                payload={
                    "source": inst.source,
                    "source_label": _SOURCE_LABELS.get(inst.source, inst.source),
                    "public_id": inst.public_id,
                    "card": _card_brief(card, rarity),
                },
            )
        )
    return out


async def _fetch_trade_events(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int,
) -> list[_FeedRow]:
    stmt = (
        select(TradeSession)
        .where(
            TradeSession.status == TRADE_STATUS_COMPLETED,
            or_(
                TradeSession.initiator_id == user_id,
                TradeSession.partner_id == user_id,
            ),
        )
        .order_by(desc(TradeSession.id))
        .limit(limit)
    )
    rows = (await session.scalars(stmt)).all()
    out: list[_FeedRow] = []
    for ts in rows:
        is_initiator = int(ts.initiator_id) == user_id
        partner_id = int(ts.partner_id if is_initiator else ts.initiator_id)
        gave_cards = (
            len(ts.initiator_card_ids or [])
            if is_initiator
            else len(ts.partner_card_ids or [])
        )
        got_cards = (
            len(ts.partner_card_ids or [])
            if is_initiator
            else len(ts.initiator_card_ids or [])
        )
        gave_pd = int(ts.initiator_pokedollars if is_initiator else ts.partner_pokedollars)
        got_pd = int(ts.partner_pokedollars if is_initiator else ts.initiator_pokedollars)
        gave_cr = int(ts.initiator_crystals if is_initiator else ts.partner_crystals)
        got_cr = int(ts.partner_crystals if is_initiator else ts.initiator_crystals)
        at = ts.created_at
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        out.append(
            _FeedRow(
                kind="trade",
                at=at,
                payload={
                    "trade_id": int(ts.id),
                    "partner_discord_id": str(partner_id),
                    "cards_received": got_cards,
                    "cards_sent": gave_cards,
                    "pokedollars_received": got_pd,
                    "pokedollars_sent": gave_pd,
                    "crystals_received": got_cr,
                    "crystals_sent": gave_cr,
                },
            )
        )
    return out


async def _fetch_auction_events(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int,
) -> list[_FeedRow]:
    inst = aliased(UserCardInstance)
    stmt = (
        select(CardAuction, inst, Card, RarityClass)
        .join(inst, CardAuction.instance_id == inst.id)
        .join(Card, inst.card_id == Card.id)
        .outerjoin(RarityClass, Card.rarity_class_id == RarityClass.id)
        .where(
            CardAuction.status == AUCTION_STATUS_ENDED_SOLD,
            or_(
                CardAuction.high_bidder_discord_id == user_id,
                CardAuction.seller_discord_id == user_id,
            ),
        )
        .order_by(desc(CardAuction.ends_at))
        .limit(limit)
    )
    rows = await session.execute(stmt)
    out: list[_FeedRow] = []
    for auc, instance, card, rarity in rows.all():
        won = int(auc.high_bidder_discord_id or 0) == user_id
        at = auc.ends_at
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        amount = int(auc.high_bid_pokedollars or auc.price_pokedollars)
        out.append(
            _FeedRow(
                kind="auction_won" if won else "auction_sold",
                at=at,
                payload={
                    "auction_id": int(auc.id),
                    "amount": amount,
                    "currency": auc.bid_currency,
                    "counterparty_discord_id": str(
                        auc.seller_discord_id if won else (auc.high_bidder_discord_id or 0)
                    ),
                    "public_id": instance.public_id,
                    "card": _card_brief(card, rarity),
                },
            )
        )
    return out


async def _fetch_mission_events(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int,
) -> list[_FeedRow]:
    stmt = (
        select(UserMission)
        .where(
            UserMission.discord_user_id == user_id,
            UserMission.claimed_at.is_not(None),
        )
        .order_by(desc(UserMission.claimed_at))
        .limit(limit)
    )
    missions = (await session.scalars(stmt)).all()
    svc = MissionService()
    out: list[_FeedRow] = []
    for m in missions:
        at = m.claimed_at
        assert at is not None
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        out.append(
            _FeedRow(
                kind="mission",
                at=at,
                payload={
                    "mission_id": int(m.id),
                    "period_type": m.period_type,
                    "reward_crystals": int(m.reward_crystals),
                    "description": svc.describe_mission_plain(m),
                },
            )
        )
    return out


async def build_luck_context(
    session: AsyncSession,
    *,
    weekend_luck_enabled: bool,
    weekend_luck_percent: int,
    weekend_luck_timezone: str,
) -> dict[str, Any]:
    effects = await resolve_active_effects(
        session,
        weekend_luck_enabled=weekend_luck_enabled,
        weekend_luck_percent=weekend_luck_percent,
        weekend_luck_timezone=weekend_luck_timezone,
    )
    global_pct = await resolve_active_rarity_luck_boost(session, None)
    row = await get_luck_boost_row(session, guild_id=None)
    scheduled_weekend = (
        row is not None
        and row.updated_by_discord_user_id == SCHEDULED_LUCK_UPDATED_BY
        and global_pct != 0
    )
    label = None
    if effects.luck_percent > 0:
        if effects.luck_source_title:
            label = (
                f"{effects.luck_source_title} — +{int(effects.luck_percent)}% rarer pulls"
            )
        else:
            label = f"Global rarity luck: {effects.luck_percent:+d}%"
    elif global_pct != 0:
        label = f"Global rarity luck: {global_pct:+d}%"
    game_events = [
        {
            "id": int(ev.id),
            "kind": ev.kind,
            "title": ev.title,
        }
        for ev in effects.live_events
        if ev.kind != EVENT_KIND_LUCK_BOOST
    ]

    return {
        "weekend_active": scheduled_weekend and weekend_luck_enabled,
        "weekend_luck_percent": int(weekend_luck_percent),
        "global_luck_percent": int(global_pct),
        "scheduled_weekend_boost": scheduled_weekend,
        "label": label,
        "daily_multiplier": int(effects.daily_multiplier),
        "free_spotlight": bool(effects.free_spotlight),
        "set_spotlight_codes": sorted(effects.set_spotlight_codes),
        "live_game_events": game_events,
    }


async def fetch_user_activity_feed(
    session: AsyncSession,
    discord_user_id: int,
    *,
    limit: int = _DEFAULT_LIMIT,
    weekend_luck_enabled: bool = True,
    weekend_luck_percent: int = 100,
    weekend_luck_timezone: str = "Europe/Stockholm",
) -> dict[str, Any]:
    """Merge recent drops, trades, auctions, and mission claims for one user."""
    cap = max(1, min(int(limit), _MAX_LIMIT))
    per = max(cap, _PER_SOURCE_LIMIT)

    acquire = await _fetch_acquire_events(session, discord_user_id, limit=per)
    trades = await _fetch_trade_events(session, discord_user_id, limit=per)
    auctions = await _fetch_auction_events(session, discord_user_id, limit=per)
    missions = await _fetch_mission_events(session, discord_user_id, limit=per)

    merged = sorted(
        acquire + trades + auctions + missions,
        key=lambda r: r.at,
        reverse=True,
    )[:cap]

    luck = await build_luck_context(
        session,
        weekend_luck_enabled=weekend_luck_enabled,
        weekend_luck_percent=weekend_luck_percent,
        weekend_luck_timezone=weekend_luck_timezone,
    )

    return {
        "items": [
            {
                "kind": row.kind,
                "at": _utc_iso(row.at),
                **row.payload,
            }
            for row in merged
        ],
        "luck": luck,
    }
