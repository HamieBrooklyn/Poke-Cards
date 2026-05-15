"""Find Discord users visible to the bot for web trade invites.

Uses :meth:`discord.Guild.query_members` (gateway request) across guilds the bot is in.
Results are limited to members the bot can see — i.e. users who share at least one server
with the bot (and where the bot has usable member resolution for that guild).
"""

from __future__ import annotations

import logging
from typing import Any

import discord

_LOG = logging.getLogger(__name__)

MIN_QUERY_LEN = 1
DEFAULT_RESULT_LIMIT = 15
MAX_RESULT_LIMIT = 25
DEFAULT_MAX_GUILDS = 28
RESOLVE_MAX_GUILDS = 55
PER_GUILD_QUERY_LIMIT = 20


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

    for guild in _guilds_for_search(bot)[:max_guilds]:
        if len(out) >= limit:
            break
        try:
            members = await guild.query_members(query=prefix, limit=PER_GUILD_QUERY_LIMIT)
        except (discord.Forbidden, discord.HTTPException, RuntimeError, OSError) as exc:
            _LOG.debug("trade user search: guild %s failed: %s", guild.id, exc)
            continue
        for m in members:
            if m.bot or m.id == requester_id or m.id in seen:
                continue
            seen.add(m.id)
            out.append(_serialize_member(m))
            if len(out) >= limit:
                break
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
    for guild in _guilds_for_search(bot)[:RESOLVE_MAX_GUILDS]:
        try:
            members = await guild.query_members(query=needle, limit=PER_GUILD_QUERY_LIMIT)
        except (discord.Forbidden, discord.HTTPException, RuntimeError, OSError):
            continue
        for m in members:
            if m.bot or m.id == requester_id:
                continue
            if m.name.lower() != needle:
                continue
            matches[m.id] = m
    if len(matches) == 1:
        return int(next(iter(matches)))
    return None
