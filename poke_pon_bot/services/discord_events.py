"""Discord guild scheduled events for the public website events panel."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import discord

_LOG = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 12.0
_HTTP_FETCH_TIMEOUT_SECONDS = 4.0
_CACHE_TTL_SECONDS = 45.0

_LIVE_STATUSES = frozenset(
    {
        discord.EventStatus.scheduled,
        discord.EventStatus.active,
    }
)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _event_url(*, invite_code: str, event_id: int) -> str:
    code = invite_code.strip().lstrip("/")
    return f"https://discord.gg/{code}?event={int(event_id)}"


def _event_location(event: discord.ScheduledEvent) -> str | None:
    loc = (getattr(event, "location", None) or "").strip()
    if loc:
        return loc
    channel = getattr(event, "channel", None)
    if channel is not None:
        return f"#{channel.name}"
    return None


def _is_current_event(event: discord.ScheduledEvent, *, now: datetime) -> bool:
    status = event.status
    if status not in _LIVE_STATUSES:
        return False
    end = event.end_time
    if end is not None and now >= _utc(end):
        return False
    return True


def _effective_status(
    event: discord.ScheduledEvent,
    *,
    now: datetime,
) -> str:
    """Discord may still report ``scheduled`` briefly after start — infer live from times."""
    if event.status == discord.EventStatus.active:
        return "active"
    start = event.start_time
    end = event.end_time
    if start is not None and now >= _utc(start):
        if end is None or now < _utc(end):
            return "active"
    return event.status.name if event.status is not None else "scheduled"


def _serialize_event(event: discord.ScheduledEvent, *, invite_code: str) -> dict[str, Any]:
    start = event.start_time
    end = event.end_time
    cover = getattr(event, "cover_image", None) or getattr(event, "cover", None)
    image_url = cover.url if cover is not None else None
    now = datetime.now(UTC)
    status_name = _effective_status(event, now=now)

    return {
        "id": str(event.id),
        "kind": "discord",
        "name": event.name,
        "description": (event.description or "").strip() or None,
        "status": status_name,
        "start_at": _utc(start).isoformat() if start is not None else None,
        "end_at": _utc(end).isoformat() if end is not None else None,
        "location": _event_location(event),
        "image_url": image_url,
        "url": _event_url(invite_code=invite_code, event_id=event.id),
    }


def _cache_get(bot: discord.Client) -> list[dict[str, Any]] | None:
    cached = getattr(bot, "_pokepon_events_cache", None)
    if not cached:
        return None
    ts, payload = cached
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        return None
    return payload


def _cache_set(bot: discord.Client, payload: list[dict[str, Any]]) -> None:
    bot._pokepon_events_cache = (time.monotonic(), payload)  # type: ignore[attr-defined]


def _stale_cache(bot: discord.Client) -> list[dict[str, Any]] | None:
    cached = getattr(bot, "_pokepon_events_cache", None)
    if not cached:
        return None
    return cached[1]


def _refresh_lock(bot: discord.Client) -> asyncio.Lock:
    lock = getattr(bot, "_pokepon_events_refresh_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        bot._pokepon_events_refresh_lock = lock  # type: ignore[attr-defined]
    return lock


def _schedule_events_refresh(bot: discord.Client, settings: Any) -> None:
    task = getattr(bot, "_pokepon_events_refresh_task", None)
    if task is not None and not task.done():
        return

    async def _run() -> None:
        await refresh_discord_events_cache(bot, settings)

    bot._pokepon_events_refresh_task = asyncio.create_task(  # type: ignore[attr-defined]
        _run(),
        name="refresh-discord-events",
    )


async def refresh_discord_events_cache(bot: discord.Client, settings: Any) -> None:
    """Fetch guild events and update the in-memory cache (one flight at a time)."""
    if not bot.is_ready():
        return

    async with _refresh_lock(bot):
        try:
            payload = await asyncio.wait_for(
                _fetch_events_uncached(bot, settings),
                timeout=_FETCH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _LOG.warning(
                "Discord events: background refresh timed out after %ss",
                _FETCH_TIMEOUT_SECONDS,
            )
            return
        except Exception:
            _LOG.exception("Discord events: background refresh failed")
            return

        _cache_set(bot, payload)


async def prefetch_discord_events_cache(bot: discord.Client, settings: Any) -> None:
    """Warm the events cache on startup so HTTP handlers stay fast."""
    await refresh_discord_events_cache(bot, settings)


async def _fetch_events_uncached(
    bot: discord.Client,
    settings: Any,
) -> list[dict[str, Any]]:
    guild_id = getattr(settings, "discord_events_guild_id", None)
    invite_code = (getattr(settings, "discord_events_invite_code", None) or "").strip()
    if guild_id is None or not invite_code:
        _LOG.info("Discord events disabled: set DISCORD_EVENTS_GUILD_ID and DISCORD_EVENTS_INVITE_CODE.")
        return []

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        in_guilds = [g.id for g in bot.guilds]
        _LOG.warning(
            "Discord events: bot is not in guild %s (bot is in %s guild(s)). "
            "Invite the bot to the official server or fix DISCORD_EVENTS_GUILD_ID.",
            guild_id,
            len(in_guilds),
        )
        return []

    try:
        events = await guild.fetch_scheduled_events()
    except discord.Forbidden:
        _LOG.warning(
            "Discord events: missing permission in guild %s (%s). "
            "Give the bot **View Channel** on the event's voice/stage channel, or **Manage Events**.",
            guild_id,
            guild.name,
        )
        return []
    except discord.HTTPException:
        _LOG.exception(
            "Discord events: fetch_scheduled_events failed guild=%s (%s)",
            guild_id,
            guild.name,
        )
        return []

    now = datetime.now(UTC)
    payload = [
        _serialize_event(ev, invite_code=invite_code)
        for ev in events
        if _is_current_event(ev, now=now)
    ]
    payload.sort(key=lambda row: row.get("start_at") or "")
    if payload:
        _LOG.info(
            "Discord events: %s upcoming/live in guild %s (%s)",
            len(payload),
            guild_id,
            guild.name,
        )
    elif events:
        statuses = ", ".join(sorted({ev.status.name for ev in events if ev.status}))
        _LOG.info(
            "Discord events: 0 upcoming/live after filter (raw=%s, statuses=%s) in guild %s (%s)",
            len(events),
            statuses or "unknown",
            guild_id,
            guild.name,
        )
    else:
        _LOG.info(
            "Discord events: Discord returned 0 scheduled events for guild %s (%s)",
            guild_id,
            guild.name,
        )
    return payload


async def fetch_public_discord_events(bot: discord.Client, settings: Any) -> list[dict[str, Any]]:
    """Return upcoming/active scheduled events (fast path for HTTP handlers)."""
    if not bot.is_ready():
        return _stale_cache(bot) or []

    cached = _cache_get(bot)
    if cached is not None:
        return cached

    stale = _stale_cache(bot)
    if stale is not None:
        _schedule_events_refresh(bot, settings)
        return stale

    try:
        payload = await asyncio.wait_for(
            _fetch_events_uncached(bot, settings),
            timeout=_HTTP_FETCH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        _LOG.warning(
            "Discord events: HTTP fetch timed out after %ss; refreshing in background",
            _HTTP_FETCH_TIMEOUT_SECONDS,
        )
        _schedule_events_refresh(bot, settings)
        return _stale_cache(bot) or []

    _cache_set(bot, payload)
    return payload
