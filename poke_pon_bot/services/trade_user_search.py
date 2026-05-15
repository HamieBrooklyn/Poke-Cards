"""Find Discord users visible to the bot for web trade invites.

Runs :meth:`discord.Guild.query_members` in parallel batches across the largest
guilds first, then merges and dedupes (gateway requests; capped per guild).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

_LOG = logging.getLogger(__name__)

MIN_QUERY_LEN = 1
DEFAULT_RESULT_LIMIT = 15
MAX_RESULT_LIMIT = 25
DEFAULT_MAX_GUILDS = 24
RESOLVE_MAX_GUILDS = 40
PER_GUILD_QUERY_LIMIT = 20
# ``query_members`` per guild is independent; run a bounded number in parallel (not 1-by-1).
GUILD_QUERY_BATCH = 14


def _serialize_member(m: discord.Member) -> dict[str, Any]:
    avatar = m.display_avatar.url if m.display_avatar else None
    return {
        "id": str(m.id),
        "username": m.name,
        "global_name": m.global_name,
        "display_name": m.display_name,
        "avatar_url": avatar,
    }


def _guilds_for_search(bot: discord.Client) -> list[discord.Guild]:
    """Guilds where the bot member exists (always true for ``bot.guilds``)."""
    guilds = list(bot.guilds)
    guilds.sort(key=lambda g: g.member_count or 0, reverse=True)
    return guilds


async def _query_members_safe(
    guild: discord.Guild, *, query: str, limit: int
) -> list[discord.Member]:
    try:
        return list(await guild.query_members(query=query, limit=limit))
    except (discord.Forbidden, discord.HTTPException, RuntimeError, OSError) as exc:
        _LOG.debug("trade user search: guild %s failed: %s", guild.id, exc)
        return []


async def search_members_shared_with_bot(
    bot: discord.Client,
    *,
    query: str,
    requester_id: int,
    limit: int = DEFAULT_RESULT_LIMIT,
    max_guilds: int = DEFAULT_MAX_GUILDS,
) -> list[dict[str, Any]]:
    """Prefix-search usernames / nicknames / display names across bot guilds.

    Discord matches a **prefix** of the query string (case-insensitive). We dedupe
    by user id and cap the total list size.
    """
    prefix = query.strip().lstrip("@").strip()
    if len(prefix) < MIN_QUERY_LEN:
        return []
    if not getattr(bot, "is_ready", lambda: True)():
        return []

    limit = max(1, min(int(limit), MAX_RESULT_LIMIT))
    max_guilds = max(1, min(int(max_guilds), 80))

    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    guild_list = _guilds_for_search(bot)[:max_guilds]

    for batch_start in range(0, len(guild_list), GUILD_QUERY_BATCH):
        batch = guild_list[batch_start : batch_start + GUILD_QUERY_BATCH]
        chunk = await asyncio.gather(
            *(
                _query_members_safe(g, query=prefix, limit=PER_GUILD_QUERY_LIMIT)
                for g in batch
            ),
            return_exceptions=True,
        )
        for res in chunk:
            if isinstance(res, BaseException):
                _LOG.debug("trade user search gather: %s", res)
                continue
            for m in res:
                if m.bot or m.id == requester_id or m.id in seen:
                    continue
                seen.add(m.id)
                out.append(_serialize_member(m))
                if len(out) >= limit:
                    return out[:limit]
    return out


async def resolve_username_in_bot_guilds(
    bot: discord.Client,
    *,
    username: str,
    requester_id: int,
) -> int | None:
    """Resolve a typed handle to a single user id, or ``None`` if ambiguous / not found.

    Uses the same prefix search, then keeps only members whose **login username**
    (``Member.name``) matches exactly (case-insensitive). Discord usernames are unique
    per user, so at most one id should match.
    """
    needle = username.strip().lstrip("@").lower()
    if not needle:
        return None
    if not getattr(bot, "is_ready", lambda: True)():
        return None

    matches: dict[int, discord.Member] = {}
    guild_list = _guilds_for_search(bot)[:RESOLVE_MAX_GUILDS]
    for batch_start in range(0, len(guild_list), GUILD_QUERY_BATCH):
        batch = guild_list[batch_start : batch_start + GUILD_QUERY_BATCH]
        chunk = await asyncio.gather(
            *(
                _query_members_safe(g, query=needle, limit=PER_GUILD_QUERY_LIMIT)
                for g in batch
            ),
            return_exceptions=True,
        )
        for res in chunk:
            if isinstance(res, BaseException):
                continue
            for m in res:
                if m.bot or m.id == requester_id:
                    continue
                if m.name.lower() != needle:
                    continue
                matches[m.id] = m
        if len(matches) > 1:
            return None
    if len(matches) == 1:
        return int(next(iter(matches)))
    return None
