"""Automatic global +luck% on Fri evening through Sun evening (official weekend event)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.services.event_scheduler import (
    is_weekend_luck_window_active,
    sync_scheduled_luck_boost,
)

__all__ = ("is_weekend_luck_window_active", "sync_weekend_luck_boost")


async def sync_weekend_luck_boost(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    enabled: bool,
    luck_percent: int,
    timezone: str,
) -> str | None:
    """Apply or clear scheduled global luck (DB events + env weekend fallback)."""
    return await sync_scheduled_luck_boost(
        session_factory,
        weekend_luck_enabled=enabled,
        weekend_luck_percent=luck_percent,
        weekend_luck_timezone=timezone,
    )
