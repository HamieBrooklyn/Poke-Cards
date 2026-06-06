"""Guild / server milestones — pack counts, server leaderboards, optional top roles."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import discord
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.guild_member_seen import GuildMemberSeen
from poke_pon_bot.models.guild_milestone import (
    GuildMemberStats,
    GuildMilestoneSettings,
    GuildStats,
)
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.referrals import mark_first_guild_join
from poke_pon_bot.services.set_chase import progress_bar

_LEADERBOARD_LIMIT = 500  # keep in sync with leaderboard.MAX_ENTRIES

_LOG = logging.getLogger(__name__)

PACK_MILESTONE_TIERS: tuple[int, ...] = (100, 250, 500, 1000, 2500, 5000, 10_000)

GUILD_STAT_CATEGORIES = frozenset({"packs", "traders", "collectors"})

GUILD_STAT_TITLES = {
    "packs": "Most packs opened",
    "traders": "Top traders",
    "collectors": "Top collectors",
}


@dataclass(frozen=True)
class MilestoneStatus:
    packs_opened: int
    trades_completed: int
    next_tier: int | None
    next_tier_progress_percent: float
    last_announced_tier: int | None
    tiers: tuple[int, ...]


async def guild_member_ids_from_db(session: AsyncSession, guild_id: int) -> set[int]:
    rows = (
        await session.execute(
            select(GuildMemberSeen.discord_user_id).where(
                GuildMemberSeen.guild_id == int(guild_id)
            )
        )
    ).scalars()
    return {int(uid) for uid in rows}


async def guild_member_ids_from_stats(session: AsyncSession, guild_id: int) -> set[int]:
    rows = (
        await session.execute(
            select(GuildMemberStats.discord_user_id).where(
                GuildMemberStats.guild_id == int(guild_id)
            )
        )
    ).scalars()
    return {int(uid) for uid in rows}


async def resolve_guild_member_ids(
    bot: discord.Client,
    session: AsyncSession,
    guild_id: int,
) -> set[int]:
    """Member IDs for server-scoped leaderboards — Discord cache first, then activity tables."""
    gid = int(guild_id)
    ids: set[int] = set()
    guild = bot.get_guild(gid)
    if guild is not None:
        ids = {int(m.id) for m in guild.members if not m.bot}
        if not ids:
            try:
                if not guild.chunked:
                    await guild.chunk()
                ids = {int(m.id) for m in guild.members if not m.bot}
            except (discord.HTTPException, discord.Forbidden, RuntimeError, OSError) as exc:
                _LOG.debug("guild chunk failed guild=%s: %s", gid, exc)

    ids |= await guild_member_ids_from_db(session, gid)
    ids |= await guild_member_ids_from_stats(session, gid)
    return ids


async def _get_or_create_guild_stats(session: AsyncSession, guild_id: int) -> GuildStats:
    row = await session.get(GuildStats, int(guild_id))
    if row is None:
        row = GuildStats(guild_id=int(guild_id))
        session.add(row)
        await session.flush()
    return row


async def _get_or_create_member_stats(
    session: AsyncSession, *, guild_id: int, discord_user_id: int
) -> GuildMemberStats:
    key = (int(guild_id), int(discord_user_id))
    row = await session.get(GuildMemberStats, key)
    if row is None:
        row = GuildMemberStats(guild_id=int(guild_id), discord_user_id=int(discord_user_id))
        session.add(row)
        await session.flush()
    return row


async def record_pack_opened(
    session: AsyncSession,
    *,
    guild_id: int,
    discord_user_id: int,
) -> list[int]:
    """Increment server pack counters. Returns newly crossed milestone tier values."""
    await mark_first_guild_join(session, guild_id=int(guild_id), discord_user_id=int(discord_user_id))
    g = await _get_or_create_guild_stats(session, int(guild_id))
    m = await _get_or_create_member_stats(
        session, guild_id=int(guild_id), discord_user_id=int(discord_user_id)
    )
    g.packs_opened = int(g.packs_opened) + 1
    m.packs_opened = int(m.packs_opened) + 1
    return _newly_crossed_tiers(g)


async def record_trade_completed(
    session: AsyncSession,
    *,
    guild_id: int,
    initiator_id: int,
    partner_id: int,
) -> None:
    g = await _get_or_create_guild_stats(session, int(guild_id))
    g.trades_completed = int(g.trades_completed) + 1
    for uid in (int(initiator_id), int(partner_id)):
        await mark_first_guild_join(session, guild_id=int(guild_id), discord_user_id=uid)
        row = await _get_or_create_member_stats(
            session, guild_id=int(guild_id), discord_user_id=uid
        )
        row.trades_completed = int(row.trades_completed) + 1


def _newly_crossed_tiers(guild_stats: GuildStats) -> list[int]:
    total = int(guild_stats.packs_opened)
    last_idx = int(guild_stats.last_milestone_tier_index)
    crossed: list[int] = []
    for i, tier in enumerate(PACK_MILESTONE_TIERS, start=1):
        if i <= last_idx:
            continue
        if total >= tier:
            crossed.append(tier)
            guild_stats.last_milestone_tier_index = i
        else:
            break
    return crossed


def build_milestone_status(guild_stats: GuildStats | None) -> MilestoneStatus:
    packs = int(guild_stats.packs_opened) if guild_stats is not None else 0
    trades = int(guild_stats.trades_completed) if guild_stats is not None else 0
    last_idx = int(guild_stats.last_milestone_tier_index) if guild_stats is not None else 0
    last_announced = (
        PACK_MILESTONE_TIERS[last_idx - 1] if last_idx > 0 and last_idx <= len(PACK_MILESTONE_TIERS) else None
    )
    next_tier: int | None = None
    pct = 100.0
    for tier in PACK_MILESTONE_TIERS:
        if packs < tier:
            next_tier = tier
            prev = 0
            for t in PACK_MILESTONE_TIERS:
                if t >= tier:
                    break
                prev = t
            span = max(1, tier - prev)
            pct = ((packs - prev) / span) * 100.0
            break
    return MilestoneStatus(
        packs_opened=packs,
        trades_completed=trades,
        next_tier=next_tier,
        next_tier_progress_percent=round(pct, 1),
        last_announced_tier=last_announced,
        tiers=PACK_MILESTONE_TIERS,
    )


async def get_milestone_status(session: AsyncSession, guild_id: int) -> MilestoneStatus:
    row = await session.get(GuildStats, int(guild_id))
    return build_milestone_status(row)


async def get_settings(session: AsyncSession, guild_id: int) -> GuildMilestoneSettings | None:
    return await session.get(GuildMilestoneSettings, int(guild_id))


async def upsert_settings(
    session: AsyncSession,
    *,
    guild_id: int,
    announcement_channel_id: int | None = None,
    top_trader_role_id: int | None = None,
    top_collector_role_id: int | None = None,
    auto_roles_enabled: bool | None = None,
    clear_announcement_channel: bool = False,
    clear_trader_role: bool = False,
    clear_collector_role: bool = False,
) -> GuildMilestoneSettings:
    row = await session.get(GuildMilestoneSettings, int(guild_id))
    if row is None:
        row = GuildMilestoneSettings(guild_id=int(guild_id))
        session.add(row)
    if clear_announcement_channel:
        row.announcement_channel_id = None
    elif announcement_channel_id is not None:
        row.announcement_channel_id = int(announcement_channel_id)
    if clear_trader_role:
        row.top_trader_role_id = None
    elif top_trader_role_id is not None:
        row.top_trader_role_id = int(top_trader_role_id)
    if clear_collector_role:
        row.top_collector_role_id = None
    elif top_collector_role_id is not None:
        row.top_collector_role_id = int(top_collector_role_id)
    if auto_roles_enabled is not None:
        row.auto_roles_enabled = bool(auto_roles_enabled)
    await session.flush()
    return row


def serialize_settings(row: GuildMilestoneSettings | None) -> dict[str, Any]:
    if row is None:
        return {
            "announcement_channel_id": None,
            "top_trader_role_id": None,
            "top_collector_role_id": None,
            "auto_roles_enabled": False,
        }
    return {
        "announcement_channel_id": (
            str(row.announcement_channel_id) if row.announcement_channel_id is not None else None
        ),
        "top_trader_role_id": (
            str(row.top_trader_role_id) if row.top_trader_role_id is not None else None
        ),
        "top_collector_role_id": (
            str(row.top_collector_role_id) if row.top_collector_role_id is not None else None
        ),
        "auto_roles_enabled": bool(row.auto_roles_enabled),
    }


def serialize_milestone_status(status: MilestoneStatus) -> dict[str, Any]:
    return {
        "packs_opened": status.packs_opened,
        "trades_completed": status.trades_completed,
        "next_tier": status.next_tier,
        "next_tier_progress_percent": status.next_tier_progress_percent,
        "last_announced_tier": status.last_announced_tier,
        "tiers": list(status.tiers),
        "progress_bar": progress_bar(status.next_tier_progress_percent),
    }


async def leaderboard_packs_opened(
    session: AsyncSession,
    member_ids: set[int],
    *,
    guild_id: int,
) -> list[tuple[int, int]]:
    if not member_ids:
        return []
    rows = (
        await session.execute(
            select(GuildMemberStats.discord_user_id, GuildMemberStats.packs_opened)
            .where(
                GuildMemberStats.guild_id == int(guild_id),
                GuildMemberStats.discord_user_id.in_(member_ids),
                GuildMemberStats.packs_opened > 0,
            )
            .order_by(desc(GuildMemberStats.packs_opened))
            .limit(_LEADERBOARD_LIMIT)
        )
    ).all()
    return [(int(uid), int(count)) for uid, count in rows]


async def leaderboard_traders(
    session: AsyncSession,
    member_ids: set[int],
    *,
    guild_id: int,
) -> list[tuple[int, int]]:
    if not member_ids:
        return []
    rows = (
        await session.execute(
            select(GuildMemberStats.discord_user_id, GuildMemberStats.trades_completed)
            .where(
                GuildMemberStats.guild_id == int(guild_id),
                GuildMemberStats.discord_user_id.in_(member_ids),
                GuildMemberStats.trades_completed > 0,
            )
            .order_by(desc(GuildMemberStats.trades_completed))
            .limit(_LEADERBOARD_LIMIT)
        )
    ).all()
    return [(int(uid), int(count)) for uid, count in rows]


async def leaderboard_collectors(
    session: AsyncSession,
    member_ids: set[int],
) -> list[tuple[int, int]]:
    if not member_ids:
        return []
    rows = (
        await session.execute(
            select(
                UserCardInstance.discord_user_id,
                func.count(func.distinct(UserCardInstance.card_id)).label("uniq"),
            )
            .where(UserCardInstance.discord_user_id.in_(member_ids))
            .group_by(UserCardInstance.discord_user_id)
            .order_by(desc("uniq"))
            .limit(_LEADERBOARD_LIMIT)
        )
    ).all()
    return [(int(uid), int(count)) for uid, count in rows if int(count) > 0]


async def fetch_guild_stat_leaderboard(
    session: AsyncSession,
    category: str,
    *,
    guild_id: int,
    member_ids: set[int],
) -> list[tuple]:
    if category == "packs":
        return await leaderboard_packs_opened(session, member_ids, guild_id=guild_id)
    if category == "traders":
        return await leaderboard_traders(session, member_ids, guild_id=guild_id)
    if category == "collectors":
        return await leaderboard_collectors(session, member_ids)
    raise ValueError(f"unknown guild stat category: {category}")


async def announce_milestone_tiers(
    bot: discord.Client,
    *,
    guild_id: int,
    crossed_tiers: list[int],
) -> None:
    if not crossed_tiers:
        return
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return
    async with bot.async_session_factory() as session:
        settings = await get_settings(session, int(guild_id))
    if settings is None or settings.announcement_channel_id is None:
        return
    channel = guild.get_channel(int(settings.announcement_channel_id))
    if not isinstance(channel, discord.TextChannel):
        return
    for tier in crossed_tiers:
        try:
            await channel.send(
                f"🎉 **Server milestone!** This community has opened **{tier:,}** packs "
                f"(`/cd` and boosters). Keep going!"
            )
        except discord.HTTPException:
            _LOG.warning("Could not announce milestone %s in guild %s", tier, guild_id)


async def sync_top_member_roles(
    bot: discord.Client,
    guild: discord.Guild,
    *,
    settings: GuildMilestoneSettings,
    member_ids: set[int],
) -> None:
    if not settings.auto_roles_enabled:
        return
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return

    async with bot.async_session_factory() as session:
        traders = await leaderboard_traders(session, member_ids, guild_id=guild.id)
        collectors = await leaderboard_collectors(session, member_ids)

    await _apply_top_role(guild, role_id=settings.top_trader_role_id, top_user_id=traders[0][0] if traders else None)
    await _apply_top_role(
        guild, role_id=settings.top_collector_role_id, top_user_id=collectors[0][0] if collectors else None
    )


async def _apply_top_role(
    guild: discord.Guild,
    *,
    role_id: int | None,
    top_user_id: int | None,
) -> None:
    if role_id is None:
        return
    role = guild.get_role(int(role_id))
    if role is None or role >= guild.me.top_role:  # type: ignore[union-attr]
        return
    for member in guild.members:
        if member.bot:
            continue
        has_role = role in member.roles
        should_have = top_user_id is not None and member.id == int(top_user_id)
        if has_role and not should_have:
            try:
                await member.remove_roles(role, reason="Poké Pon top guild role rotation")
            except discord.HTTPException:
                pass
        elif should_have and not has_role:
            try:
                await member.add_roles(role, reason="Poké Pon top guild member")
            except discord.HTTPException:
                pass


def milestone_embed(guild_name: str, status: MilestoneStatus) -> discord.Embed:
    bar = progress_bar(status.next_tier_progress_percent)
    if status.next_tier is None:
        goal_line = f"**{status.packs_opened:,}** packs opened — all milestones complete! 🏆"
    else:
        goal_line = (
            f"**{status.packs_opened:,}** / **{status.next_tier:,}** packs toward next milestone\n"
            f"`{bar}` **{status.next_tier_progress_percent:.0f}%**"
        )
    desc = (
        f"{goal_line}\n\n"
        f"**Trades completed** in this server: **{status.trades_completed:,}**\n\n"
        "Server leaderboards: **`/leaderboard`** with scope **Server** — "
        "categories **Packs opened**, **Top traders**, **Top collectors**.\n"
        "Admins: **`/servermilestones config`** for announcements and optional top roles."
    )
    return discord.Embed(title=f"Server milestones — {guild_name}", description=desc)
