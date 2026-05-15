"""Resolve Discord user display fields for web APIs (KnownUser cache + live bot lookup)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.known_user import KnownUser

_LOG = logging.getLogger(__name__)


def serialize_user_profile(
    uid: int,
    *,
    ku: KnownUser | None = None,
    discord_user: discord.User | discord.Member | None = None,
) -> dict[str, Any]:
    """JSON-safe user block for auctions, trades, bid lists, etc."""
    if ku is not None:
        return {
            "id": str(uid),
            "username": ku.username,
            "global_name": ku.global_name,
            "avatar_url": ku.avatar_url,
        }
    if discord_user is not None:
        avatar = discord_user.display_avatar.url if discord_user.display_avatar else None
        global_name = getattr(discord_user, "global_name", None)
        return {
            "id": str(uid),
            "username": discord_user.name,
            "global_name": global_name,
            "avatar_url": avatar,
        }
    return {
        "id": str(uid),
        "username": None,
        "global_name": None,
        "avatar_url": None,
    }


async def load_known_users(
    session: AsyncSession,
    user_ids: Iterable[int],
) -> dict[int, KnownUser]:
    ids = {int(i) for i in user_ids if i}
    if not ids:
        return {}
    rows = await session.execute(select(KnownUser).where(KnownUser.discord_id.in_(ids)))
    return {int(ku.discord_id): ku for ku in rows.scalars()}


async def resolve_user_profiles(
    session: AsyncSession,
    bot: Any,
    user_ids: Iterable[int],
) -> dict[int, dict[str, Any]]:
    """Load profiles for many user ids; fill gaps from the connected Discord client when possible."""
    ids = {int(i) for i in user_ids if i}
    if not ids:
        return {}

    known = await load_known_users(session, ids)
    out: dict[int, dict[str, Any]] = {}
    missing: list[int] = []

    for uid in ids:
        ku = known.get(uid)
        if ku is not None:
            out[uid] = serialize_user_profile(uid, ku=ku)
        else:
            missing.append(uid)

    for uid in missing:
        discord_user: discord.User | discord.Member | None = None
        if bot is not None:
            discord_user = bot.get_user(uid)
            if discord_user is None:
                try:
                    discord_user = await bot.fetch_user(uid)
                except (discord.HTTPException, discord.NotFound, RuntimeError, OSError) as exc:
                    _LOG.debug("fetch_user %s failed: %s", uid, exc)
        out[uid] = serialize_user_profile(uid, discord_user=discord_user)

    return out
