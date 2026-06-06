"""Manage global / per-guild rarity luck boosts."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.rarity_luck_boost import RarityLuckBoost
from poke_pon_bot.services.rarity_luck import combine_luck_percent

_LOG = logging.getLogger(__name__)

# ``updated_by_discord_user_id`` for rows written by automated schedules (not /dev).
SCHEDULED_LUCK_UPDATED_BY = 0


async def get_luck_boost_row(
    session: AsyncSession,
    *,
    guild_id: int | None,
) -> RarityLuckBoost | None:
    return await session.scalar(
        select(RarityLuckBoost).where(RarityLuckBoost.guild_id.is_(guild_id))
    )


async def resolve_active_rarity_luck_boost(
    session: AsyncSession,
    guild_id: int | None,
) -> int:
    """Guild boost wins when set; otherwise global. Returns **0** when unset."""
    row: RarityLuckBoost | None = None
    if guild_id is not None:
        row = await get_luck_boost_row(session, guild_id=guild_id)
    if row is None:
        row = await get_luck_boost_row(session, guild_id=None)
    if row is None:
        return 0
    return int(row.luck_percent)


async def resolve_effective_rarity_luck(
    session: AsyncSession,
    guild_id: int | None,
    *,
    extra_luck_percent: float = 0.0,
) -> float:
    """Active server/global boost plus optional per-command extra (e.g. ``/dev drop``)."""
    boost = await resolve_active_rarity_luck_boost(session, guild_id)
    return combine_luck_percent(boost, extra_luck_percent)


async def upsert_rarity_luck_boost(
    session: AsyncSession,
    *,
    guild_id: int | None,
    luck_percent: int,
    updated_by_discord_user_id: int,
) -> RarityLuckBoost:
    now = datetime.now(UTC)
    pct = int(luck_percent)
    row = await get_luck_boost_row(session, guild_id=guild_id)
    if row is None:
        row = RarityLuckBoost(
            guild_id=guild_id,
            luck_percent=pct,
            updated_at=now,
            updated_by_discord_user_id=updated_by_discord_user_id,
        )
        session.add(row)
    else:
        row.luck_percent = pct
        row.updated_at = now
        row.updated_by_discord_user_id = updated_by_discord_user_id
    await session.flush()
    return row


async def clear_rarity_luck_boost(session: AsyncSession, *, guild_id: int | None) -> bool:
    result = await session.execute(
        delete(RarityLuckBoost).where(RarityLuckBoost.guild_id.is_(guild_id))
    )
    return (result.rowcount or 0) > 0


async def clear_scheduled_global_luck_boost(session: AsyncSession) -> bool:
    """Remove global boost only if the weekend scheduler owns it (leaves manual /dev rows)."""
    row = await get_luck_boost_row(session, guild_id=None)
    if row is None or row.updated_by_discord_user_id != SCHEDULED_LUCK_UPDATED_BY:
        return False
    return await clear_rarity_luck_boost(session, guild_id=None)


def format_luck_boost_line(luck_percent: int, *, scope_label: str) -> str:
    if luck_percent == 0:
        return ""
    direction = "rarer" if luck_percent > 0 else "common"
    return (
        f"🍀 **Rarity luck** ({scope_label}) — **{luck_percent:+d}%** "
        f"({direction} pulls / wild Pokémon)"
    )


async def format_stored_luck_summary(
    session: AsyncSession,
    row: RarityLuckBoost | None,
    *,
    scope_name: str,
) -> str:
    if row is None:
        return f"**{scope_name}:** *(none)*"
    pct = int(row.luck_percent)
    hint = "neutral"
    if pct > 0:
        hint = "rarer"
    elif pct < 0:
        hint = "more common"
    return f"**{scope_name}:** **{pct:+d}%** ({hint})"
