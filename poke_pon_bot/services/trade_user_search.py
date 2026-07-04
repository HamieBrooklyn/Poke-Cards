"""Find Discord users visible to the bot for web trade invites.

Fast path: ``known_users`` table (OAuth sign-ins). Slow path: ``query_members`` only in
guilds the requester shares with the bot (not every guild the bot is in).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.known_user import KnownUser

_LOG = logging.getLogger(__name__)

MIN_QUERY_LEN = 1
DEFAULT_RESULT_LIMIT = 15
MAX_RESULT_LIMIT = 25
# Discord gateway: only guilds the requester is in (usually 1–5, not 20+).
MAX_SHARED_GUILDS = 10
PER_GUILD_QUERY_LIMIT = 15
GUILD_QUERY_BATCH = 8
# Fallback when member cache has no shared guilds (cold cache).
FALLBACK_GUILD_PROBE = 6
MAX_GUILD_LIST = 30
GUILD_LIST_PROBE = 80
GUILD_LIST_FETCH_BATCH = 10
_SEARCH_CACHE_TTL_SEC = 50.0
_search_cache: dict[tuple[int, str], tuple[float, list[dict[str, Any]]]] = {}


def _serialize_member(m: discord.Member) -> dict[str, Any]:
    avatar = m.display_avatar.url if m.display_avatar else None
    return {
        "id": str(m.id),
        "username": m.name,
        "global_name": m.global_name,
        "display_name": m.display_name,
        "avatar_url": avatar,
    }


def _serialize_known_user(ku: KnownUser) -> dict[str, Any]:
    return {
        "id": str(ku.discord_id),
        "username": ku.username,
        "global_name": ku.global_name,
        "display_name": ku.global_name or ku.username,
        "avatar_url": ku.avatar_url,
    }


def _guilds_for_search(bot: discord.Client) -> list[discord.Guild]:
    guilds = list(bot.guilds)
    guilds.sort(key=lambda g: g.member_count or 0, reverse=True)
    return guilds


def _guilds_shared_with_requester(bot: discord.Client, requester_id: int) -> list[discord.Guild]:
    """Guilds where the requester is already in the member cache."""
    out: list[discord.Guild] = []
    for guild in _guilds_for_search(bot):
        if guild.get_member(requester_id) is not None:
            out.append(guild)
    return out[:MAX_SHARED_GUILDS]


async def _user_in_guild(guild: discord.Guild, user_id: int) -> bool:
    if guild.get_member(user_id) is not None:
        return True
    try:
        await guild.fetch_member(user_id)
        return True
    except discord.NotFound:
        return False
    except (discord.Forbidden, discord.HTTPException, RuntimeError, OSError) as exc:
        _LOG.debug("list_shared_guilds fetch_member guild %s: %s", guild.id, exc)
        return False


async def list_shared_guilds_for_user(
    bot: discord.Client, user_id: int,
) -> list[dict[str, Any]]:
    """Servers the user shares with the bot (member cache + parallel ``fetch_member``)."""
    seen: dict[int, discord.Guild] = {}
    guilds = _guilds_for_search(bot)
    for guild in guilds:
        if guild.get_member(user_id) is not None:
            seen[guild.id] = guild

    remaining = [g for g in guilds if g.id not in seen]
    if len(remaining) > GUILD_LIST_PROBE:
        remaining = remaining[:GUILD_LIST_PROBE]

    for i in range(0, len(remaining), GUILD_LIST_FETCH_BATCH):
        batch = remaining[i : i + GUILD_LIST_FETCH_BATCH]
        results = await asyncio.gather(
            *[_user_in_guild(g, user_id) for g in batch],
            return_exceptions=True,
        )
        for guild, ok in zip(batch, results):
            if ok is True:
                seen[guild.id] = guild

    ordered = sorted(seen.values(), key=lambda g: (g.name or "").lower())
    return [{"id": str(g.id), "name": g.name} for g in ordered[:MAX_GUILD_LIST]]


async def _probe_shared_guilds(
    bot: discord.Client, requester_id: int,
) -> list[discord.Guild]:
    """If the cache has no shared guilds, probe the largest guilds once."""
    out = _guilds_shared_with_requester(bot, requester_id)
    if out:
        return out
    for guild in _guilds_for_search(bot)[:FALLBACK_GUILD_PROBE]:
        try:
            await guild.fetch_member(requester_id)
            out.append(guild)
        except discord.NotFound:
            continue
        except (discord.Forbidden, discord.HTTPException, RuntimeError, OSError) as exc:
            _LOG.debug("fetch_member probe guild %s: %s", guild.id, exc)
    return out[:MAX_SHARED_GUILDS]


async def _query_members_safe(
    guild: discord.Guild, *, query: str, limit: int,
) -> list[discord.Member]:
    try:
        return list(await guild.query_members(query=query, limit=limit))
    except (discord.Forbidden, discord.HTTPException, RuntimeError, OSError) as exc:
        _LOG.debug("trade user search: guild %s failed: %s", guild.id, exc)
        return []


async def search_known_users_prefix(
    session: AsyncSession,
    *,
    query: str,
    requester_id: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Instant prefix match on cached OAuth users."""
    prefix = query.strip().lstrip("@").strip().lower()
    if len(prefix) < MIN_QUERY_LEN:
        return []
    pattern = prefix + "%"
    rows = (
        await session.execute(
            select(KnownUser)
            .where(
                KnownUser.discord_id != requester_id,
                or_(
                    func.lower(KnownUser.username).like(pattern),
                    func.lower(KnownUser.global_name).like(pattern),
                ),
            )
            .order_by(KnownUser.last_seen_at.desc())
            .limit(limit),
        )
    ).scalars().all()
    return [_serialize_known_user(ku) for ku in rows]


async def _discord_member_search(
    bot: discord.Client,
    *,
    prefix: str,
    requester_id: int,
    limit: int,
    exclude_ids: set[int],
) -> list[dict[str, Any]]:
    guild_list = await _probe_shared_guilds(bot, requester_id)
    if not guild_list:
        return []

    seen = set(exclude_ids)
    out: list[dict[str, Any]] = []

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
                    return out
    return out


async def search_members_shared_with_bot(
    bot: discord.Client,
    *,
    query: str,
    requester_id: int,
    limit: int = DEFAULT_RESULT_LIMIT,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[dict[str, Any]]:
    """Prefix-search for trade invite autocomplete (DB first, then shared-guild Discord)."""
    prefix = query.strip().lstrip("@").strip()
    if len(prefix) < MIN_QUERY_LEN:
        return []
    if not getattr(bot, "is_ready", lambda: True)():
        return []

    limit = max(1, min(int(limit), MAX_RESULT_LIMIT))
    cache_key = (requester_id, prefix.lower())
    now = time.monotonic()
    cached = _search_cache.get(cache_key)
    if cached is not None and now - cached[0] < _SEARCH_CACHE_TTL_SEC:
        return cached[1][:limit]

    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    if session_factory is not None:
        try:
            async with session_factory() as session:
                db_hits = await search_known_users_prefix(
                    session, query=prefix, requester_id=requester_id, limit=limit,
                )
            for row in db_hits:
                uid = int(row["id"])
                if uid in seen:
                    continue
                seen.add(uid)
                out.append(row)
                if len(out) >= limit:
                    _search_cache[cache_key] = (now, out)
                    return out
        except Exception:
            _LOG.exception("trade user search known_users q=%r", prefix)

    if len(out) < limit:
        discord_hits = await _discord_member_search(
            bot,
            prefix=prefix,
            requester_id=requester_id,
            limit=limit - len(out),
            exclude_ids=seen,
        )
        out.extend(discord_hits)

    _search_cache[cache_key] = (now, out)
    if len(_search_cache) > 500:
        cutoff = now - _SEARCH_CACHE_TTL_SEC
        for k in [k for k, (ts, _) in _search_cache.items() if ts < cutoff]:
            del _search_cache[k]

    return out[:limit]


async def resolve_username_in_bot_guilds(
    bot: discord.Client,
    *,
    username: str,
    requester_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int | None:
    """Resolve a typed handle to a single user id, or ``None`` if ambiguous / not found."""
    needle = username.strip().lstrip("@").lower()
    if not needle:
        return None
    if not getattr(bot, "is_ready", lambda: True)():
        return None

    if session_factory is not None:
        try:
            async with session_factory() as session:
                rows = (
                    await session.execute(
                        select(KnownUser.discord_id).where(
                            KnownUser.discord_id != requester_id,
                            func.lower(KnownUser.username) == needle,
                        ).limit(2),
                    )
                ).all()
            if len(rows) == 1:
                return int(rows[0][0])
            if len(rows) > 1:
                return None
        except Exception:
            _LOG.debug("resolve username known_users failed", exc_info=True)

    matches: dict[int, discord.Member] = {}
    guild_list = await _probe_shared_guilds(bot, requester_id)
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
