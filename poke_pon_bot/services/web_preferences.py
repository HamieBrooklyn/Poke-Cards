"""Load and update website notification preferences."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.user_web_preferences import UserWebPreferences
from poke_pon_bot.services.crystal_sinks import serialize_cosmetics

DEFAULTS: dict[str, bool] = {
    "notify_trades": True,
    "notify_auctions": True,
    "notify_referrals": True,
    "notify_missions": True,
    "notify_wishlist_market": True,
    "notify_browser": False,
    "notify_daily": False,
    "notify_vote": True,
    "notify_drop": False,
}

INT_DEFAULTS: dict[str, int | None] = {
    "wishlist_alert_max_pokedollars": None,
    "wishlist_alert_max_crystals": None,
}


def serialize_web_preferences(row: UserWebPreferences | None) -> dict:
    if row is None:
        return {**DEFAULTS, **INT_DEFAULTS, **serialize_cosmetics(None)}
    return {
        "notify_trades": bool(row.notify_trades),
        "notify_auctions": bool(row.notify_auctions),
        "notify_referrals": bool(row.notify_referrals),
        "notify_missions": bool(getattr(row, "notify_missions", True)),
        "notify_wishlist_market": bool(getattr(row, "notify_wishlist_market", True)),
        "notify_browser": bool(getattr(row, "notify_browser", False)),
        "notify_daily": bool(getattr(row, "notify_daily", False)),
        "notify_vote": bool(getattr(row, "notify_vote", True)),
        "notify_drop": bool(getattr(row, "notify_drop", False)),
        "wishlist_alert_max_pokedollars": getattr(row, "wishlist_alert_max_pokedollars", None),
        "wishlist_alert_max_crystals": getattr(row, "wishlist_alert_max_crystals", None),
        **serialize_cosmetics(row),
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


def _parse_optional_cap(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return value


async def update_web_preferences(
    session: AsyncSession,
    discord_user_id: int,
    patch: dict[str, Any],
) -> dict[str, bool | int | None]:
    row = await get_or_create_web_preferences(session, discord_user_id)
    for key in DEFAULTS:
        if key in patch:
            setattr(row, key, bool(patch[key]))
    for key in INT_DEFAULTS:
        if key in patch:
            setattr(row, key, _parse_optional_cap(patch[key]))
    await session.flush()
    return serialize_web_preferences(row)
