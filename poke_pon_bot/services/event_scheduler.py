"""DB-backed scheduled game events — luck, double daily, auction spotlight promos."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.scheduled_game_event import (
    EVENT_KIND_DOUBLE_DAILY,
    EVENT_KIND_FREE_SPOTLIGHT,
    EVENT_KIND_LUCK_BOOST,
    EVENT_KIND_SET_SPOTLIGHT,
    EVENT_KINDS,
    ScheduledGameEvent,
)
from poke_pon_bot.services.rarity_luck_boost import (
    SCHEDULED_LUCK_UPDATED_BY,
    clear_scheduled_global_luck_boost,
    get_luck_boost_row,
    upsert_rarity_luck_boost,
)

_LOG = logging.getLogger(__name__)

RECURRENCE_WEEKLY = "weekly"

# Friday 18:00 → Sunday 20:00 (env fallback when no DB luck event is live).
_WEEKEND_FRIDAY = 4
_WEEKEND_SATURDAY = 5
_WEEKEND_SUNDAY = 6
_WEEKEND_START_MINUTES = 18 * 60
_WEEKEND_END_MINUTES = 20 * 60


def is_weekend_luck_window_active(
    now: datetime | None = None,
    *,
    timezone: str = "Europe/Stockholm",
) -> bool:
    """True during the weekly weekend luck window in the given IANA timezone."""
    tz = ZoneInfo(timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    wd = now.weekday()
    minutes = now.hour * 60 + now.minute
    if wd == _WEEKEND_FRIDAY:
        return minutes >= _WEEKEND_START_MINUTES
    if wd == _WEEKEND_SATURDAY:
        return True
    if wd == _WEEKEND_SUNDAY:
        return minutes < _WEEKEND_END_MINUTES
    return False


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_event_config(event: ScheduledGameEvent) -> dict[str, Any]:
    raw = event.config_json or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _weekly_window_active(
    now: datetime,
    *,
    timezone: str,
    weekday_start: int,
    hour_start: int,
    minute_start: int,
    weekday_end: int,
    hour_end: int,
    minute_end: int,
) -> bool:
    tz = ZoneInfo(timezone)
    now = now.astimezone(tz)
    wd = now.weekday()
    minutes = now.hour * 60 + now.minute
    start_minutes = int(hour_start) * 60 + int(minute_start)
    end_minutes = int(hour_end) * 60 + int(minute_end)
    start_wd = int(weekday_start)
    end_wd = int(weekday_end)

    if start_wd == end_wd:
        return wd == start_wd and start_minutes <= minutes < end_minutes
    if start_wd < end_wd:
        if wd < start_wd or wd > end_wd:
            return False
        if wd == start_wd:
            return minutes >= start_minutes
        if wd == end_wd:
            return minutes < end_minutes
        return True
    # Wraps week boundary (e.g. Fri → Sun).
    if wd > start_wd or wd < end_wd:
        return True
    if wd == start_wd:
        return minutes >= start_minutes
    if wd == end_wd:
        return minutes < end_minutes
    return False


def _weekly_kwargs(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "timezone": str(cfg.get("timezone") or "Europe/Stockholm"),
        "weekday_start": int(cfg.get("weekday_start", 4)),
        "hour_start": int(cfg.get("hour_start", 18)),
        "minute_start": int(cfg.get("minute_start", 0)),
        "weekday_end": int(cfg.get("weekday_end", 6)),
        "hour_end": int(cfg.get("hour_end", 20)),
        "minute_end": int(cfg.get("minute_end", 0)),
    }


def is_event_live(event: ScheduledGameEvent, *, now: datetime | None = None) -> bool:
    if not event.enabled:
        return False
    ref = _utc(now or datetime.now(UTC))
    if event.recurrence == RECURRENCE_WEEKLY:
        cfg = parse_event_config(event)
        return _weekly_window_active(ref, **_weekly_kwargs(cfg))
    if event.starts_at is None or event.ends_at is None:
        return False
    return _utc(event.starts_at) <= ref < _utc(event.ends_at)


def event_public_window(
    event: ScheduledGameEvent,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Best-effort start/end for API/UI (absolute events only)."""
    if event.recurrence == RECURRENCE_WEEKLY:
        return None, None
    return event.starts_at, event.ends_at


def event_status(event: ScheduledGameEvent, *, now: datetime | None = None) -> str:
    ref = _utc(now or datetime.now(UTC))
    if not event.enabled:
        return "disabled"
    if is_event_live(event, now=ref):
        return "active"
    if event.recurrence == RECURRENCE_WEEKLY:
        return "scheduled"
    if event.starts_at and _utc(event.starts_at) > ref:
        return "scheduled"
    if event.ends_at and _utc(event.ends_at) <= ref:
        return "ended"
    return "scheduled"


@dataclass(frozen=True, slots=True)
class ActiveGameEffects:
    luck_percent: int = 0
    luck_source_title: str | None = None
    daily_multiplier: int = 1
    free_spotlight: bool = False
    set_spotlight_codes: frozenset[str] = frozenset()
    live_events: tuple[ScheduledGameEvent, ...] = ()


async def fetch_enabled_events(session: AsyncSession) -> list[ScheduledGameEvent]:
    rows = (
        await session.scalars(
            select(ScheduledGameEvent)
            .where(ScheduledGameEvent.enabled.is_(True))
            .order_by(ScheduledGameEvent.starts_at.asc().nulls_last(), ScheduledGameEvent.id.asc())
        )
    ).all()
    return list(rows)


async def resolve_active_effects(
    session: AsyncSession,
    *,
    weekend_luck_enabled: bool = True,
    weekend_luck_percent: int = 100,
    weekend_luck_timezone: str = "Europe/Stockholm",
) -> ActiveGameEffects:
    now = datetime.now(UTC)
    live: list[ScheduledGameEvent] = []
    luck_percent = 0
    luck_title: str | None = None
    daily_multiplier = 1
    free_spotlight = False
    set_codes: set[str] = set()

    for event in await fetch_enabled_events(session):
        if not is_event_live(event, now=now):
            continue
        live.append(event)
        cfg = parse_event_config(event)
        if event.kind == EVENT_KIND_LUCK_BOOST:
            pct = int(cfg.get("luck_percent") or 0)
            if pct > luck_percent:
                luck_percent = pct
                luck_title = event.title
        elif event.kind == EVENT_KIND_DOUBLE_DAILY:
            mult = max(1, int(cfg.get("multiplier") or 2))
            daily_multiplier = max(daily_multiplier, mult)
        elif event.kind == EVENT_KIND_FREE_SPOTLIGHT:
            free_spotlight = True
        elif event.kind == EVENT_KIND_SET_SPOTLIGHT:
            code = str(cfg.get("set_code") or "").strip().lower()
            if code:
                set_codes.add(code)

    if luck_percent == 0 and weekend_luck_enabled and is_weekend_luck_window_active(
        now=now, timezone=weekend_luck_timezone
    ):
        luck_percent = int(weekend_luck_percent)
        luck_title = f"Weekend luck ({weekend_luck_timezone})"

    return ActiveGameEffects(
        luck_percent=luck_percent,
        luck_source_title=luck_title,
        daily_multiplier=daily_multiplier,
        free_spotlight=free_spotlight,
        set_spotlight_codes=frozenset(set_codes),
        live_events=tuple(live),
    )


async def _scheduler_owns_global_luck(session: AsyncSession) -> bool:
    row = await get_luck_boost_row(session, guild_id=None)
    if row is None:
        return True
    uid = int(row.updated_by_discord_user_id or 0)
    return uid in (0, SCHEDULED_LUCK_UPDATED_BY)


async def sync_scheduled_luck_boost(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    weekend_luck_enabled: bool,
    weekend_luck_percent: int,
    weekend_luck_timezone: str,
) -> str | None:
    """Apply or clear global luck from live scheduled events (+ env weekend fallback)."""
    async with session_factory() as session:
        if not await _scheduler_owns_global_luck(session):
            return None
        effects = await resolve_active_effects(
            session,
            weekend_luck_enabled=weekend_luck_enabled,
            weekend_luck_percent=weekend_luck_percent,
            weekend_luck_timezone=weekend_luck_timezone,
        )
        pct = int(effects.luck_percent)
        if pct > 0:
            row = await upsert_rarity_luck_boost(
                session,
                guild_id=None,
                luck_percent=pct,
                updated_by_discord_user_id=SCHEDULED_LUCK_UPDATED_BY,
            )
            await session.commit()
            label = effects.luck_source_title or "scheduled event"
            return f"game event luck ON: global {int(row.luck_percent):+d}% ({label})"
        removed = await clear_scheduled_global_luck_boost(session)
        await session.commit()
        if removed:
            return "game event luck OFF"
    return None


def effects_fingerprint(effects: ActiveGameEffects) -> str:
    return (
        f"{effects.luck_percent}|{effects.daily_multiplier}|"
        f"{int(effects.free_spotlight)}|{','.join(sorted(effects.set_spotlight_codes))}"
    )


async def resolve_spotlight_crystal_cost(
    session: AsyncSession,
    *,
    auction_instance_id: int,
    weekend_luck_enabled: bool = True,
    weekend_luck_percent: int = 100,
    weekend_luck_timezone: str = "Europe/Stockholm",
) -> int:
    from poke_pon_bot.models.card import Card
    from poke_pon_bot.models.inventory import UserCardInstance
    from poke_pon_bot.services.crystal_sinks import AUCTION_SPOTLIGHT_CRYSTAL_COST

    effects = await resolve_active_effects(
        session,
        weekend_luck_enabled=weekend_luck_enabled,
        weekend_luck_percent=weekend_luck_percent,
        weekend_luck_timezone=weekend_luck_timezone,
    )
    if effects.free_spotlight:
        return 0
    if effects.set_spotlight_codes:
        inst = await session.get(UserCardInstance, int(auction_instance_id))
        if inst is not None:
            card = await session.get(Card, int(inst.card_id))
            if card is not None and (card.set_code or "").lower() in effects.set_spotlight_codes:
                return 0
    return AUCTION_SPOTLIGHT_CRYSTAL_COST


def event_to_public_json(event: ScheduledGameEvent, *, now: datetime | None = None) -> dict[str, Any]:
    ref = _utc(now or datetime.now(UTC))
    cfg = parse_event_config(event)
    start, end = event_public_window(event, now=ref)
    payload: dict[str, Any] = {
        "id": int(event.id),
        "kind": event.kind,
        "title": event.title,
        "description": event.description,
        "status": event_status(event, now=ref),
        "recurrence": event.recurrence,
        "start_at": start.isoformat() if start else None,
        "end_at": end.isoformat() if end else None,
    }
    if event.kind == EVENT_KIND_LUCK_BOOST:
        payload["luck_percent"] = int(cfg.get("luck_percent") or 0)
    elif event.kind == EVENT_KIND_DOUBLE_DAILY:
        payload["daily_multiplier"] = max(1, int(cfg.get("multiplier") or 2))
    elif event.kind == EVENT_KIND_SET_SPOTLIGHT:
        payload["set_code"] = cfg.get("set_code")
    if event.recurrence == RECURRENCE_WEEKLY:
        payload["timezone"] = cfg.get("timezone")
        payload["schedule_label"] = _weekly_schedule_label(cfg)
    return payload


def _weekly_schedule_label(cfg: dict[str, Any]) -> str:
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    start_wd = int(cfg.get("weekday_start", 4))
    end_wd = int(cfg.get("weekday_end", 6))
    sh = int(cfg.get("hour_start", 18))
    sm = int(cfg.get("minute_start", 0))
    eh = int(cfg.get("hour_end", 20))
    em = int(cfg.get("minute_end", 0))
    tz = str(cfg.get("timezone") or "Europe/Stockholm")
    return (
        f"{days[start_wd % 7]} {sh:02d}:{sm:02d} – "
        f"{days[end_wd % 7]} {eh:02d}:{em:02d} ({tz})"
    )


async def list_events_public(
    session: AsyncSession,
    *,
    include_ended: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    rows = await fetch_enabled_events(session)
    ranked: list[tuple[int, ScheduledGameEvent, str]] = []
    for row in rows:
        status = event_status(row, now=now)
        if status == "ended" and not include_ended:
            continue
        if status == "disabled":
            continue
        rank = {"active": 0, "scheduled": 1, "ended": 2}.get(status, 3)
        ranked.append((rank, row, status))
    ranked.sort(key=lambda item: (item[0], item[1].starts_at or datetime.min.replace(tzinfo=UTC)))
    out: list[dict[str, Any]] = []
    for _, row, _status in ranked:
        out.append(event_to_public_json(row, now=now))
        if len(out) >= limit:
            break
    return out


def parse_iso_datetime(raw: str) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise ValueError("datetime is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return _utc(dt)


def default_weekend_luck_config(
    *,
    luck_percent: int,
    timezone: str,
) -> dict[str, Any]:
    return {
        "luck_percent": int(luck_percent),
        "timezone": timezone,
        "weekday_start": 4,
        "hour_start": 18,
        "minute_start": 0,
        "weekday_end": 6,
        "hour_end": 20,
        "minute_end": 0,
    }


async def create_event(
    session: AsyncSession,
    *,
    kind: str,
    title: str,
    created_by_discord_user_id: int,
    description: str | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    recurrence: str | None = None,
    config: dict[str, Any] | None = None,
) -> ScheduledGameEvent:
    k = (kind or "").strip().lower()
    if k not in EVENT_KINDS:
        raise ValueError(f"Unknown event kind: {kind}")
    if recurrence == RECURRENCE_WEEKLY:
        starts_at = None
        ends_at = None
    elif starts_at is None or ends_at is None:
        raise ValueError("starts_at and ends_at are required unless recurrence is weekly")
    elif _utc(starts_at) >= _utc(ends_at):
        raise ValueError("ends_at must be after starts_at")
    row = ScheduledGameEvent(
        kind=k,
        title=(title or "").strip() or k.replace("_", " ").title(),
        description=(description or "").strip() or None,
        starts_at=starts_at,
        ends_at=ends_at,
        recurrence=recurrence,
        config_json=json.dumps(config or {}),
        enabled=True,
        created_by_discord_user_id=int(created_by_discord_user_id),
    )
    session.add(row)
    await session.flush()
    return row


async def cancel_event(session: AsyncSession, event_id: int) -> bool:
    row = await session.get(ScheduledGameEvent, int(event_id))
    if row is None:
        return False
    row.enabled = False
    await session.flush()
    return True
