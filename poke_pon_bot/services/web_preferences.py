"""Load and update website notification preferences."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.user_web_preferences import UserWebPreferences

DEFAULTS: dict[str, bool] = {
    "notify_trades": True,
    "notify_auctions": True,
    "notify_referrals": True,
}


def serialize_web_preferences(row: UserWebPreferences | None) -> dict[str, bool]:
    if row is None:
        return dict(DEFAULTS)
    return {
        "notify_trades": bool(row.notify_trades),
        "notify_auctions": bool(row.notify_auctions),
        "notify_referrals": bool(row.notify_referrals),
    }


async def get_or_create_web_preferences(
    session: AsyncSession,
    discord_user_id: int,
) -> UserWebPreferences:
    row = await session.get(UserWebPreferences, int(discord_user_id))
    if row is not None:
        return row
    row = UserWebPreferences(discord_user_id=int(discord_user_id))
    session.add(row)
    await session.flush()
    return row


async def update_web_preferences(
    session: AsyncSession,
    discord_user_id: int,
    patch: dict[str, Any],
) -> dict[str, bool]:
    row = await get_or_create_web_preferences(session, discord_user_id)
    for key in DEFAULTS:
        if key in patch:
            setattr(row, key, bool(patch[key]))
    await session.flush()
    return serialize_web_preferences(row)
