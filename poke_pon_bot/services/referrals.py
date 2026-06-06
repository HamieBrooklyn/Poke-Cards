"""Automatic server-invite referrals: reward inviter and invitee when friends play."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.guild_member_seen import GuildMemberSeen
from poke_pon_bot.models.guild_referral import GuildReferral
from poke_pon_bot.services.crystals import CrystalsService, format_crystals
from poke_pon_bot.services.notification_delivery import PREF_REFERRALS, schedule_notification

_LOG = logging.getLogger(__name__)

REFERRAL_CD_USES_REQUIRED = 10
REFERRAL_CRYSTAL_REWARD = 25
REFERRAL_MAX_REWARDS_PER_INVITER = 3
REFERRAL_FIRST_PACK_INVITEE_CRYSTALS = 15
REFERRAL_FIRST_PACK_INVITER_CRYSTALS = 10
# Inviters must have an established account (anti-throwaway-inviter farming).
REFERRAL_MIN_INVITER_ACCOUNT_AGE_DAYS = 7


@dataclass(frozen=True)
class ReferralRewardNotice:
    inviter_id: int
    invitee_id: int
    crystals: int
    rewards_used: int
    rewards_cap: int = REFERRAL_MAX_REWARDS_PER_INVITER


@dataclass(frozen=True)
class ReferralFirstPackNotice:
    inviter_id: int
    invitee_id: int
    inviter_crystals: int
    invitee_crystals: int


@dataclass(frozen=True)
class ReferralCdUseResult:
    first_pack: ReferralFirstPackNotice | None = None
    threshold: ReferralRewardNotice | None = None


def referral_status_for_row(row: GuildReferral) -> str:
    """``in_progress`` | ``rewarded`` | ``completed`` (threshold met, no crystals)."""
    if row.completed_at is None:
        return "in_progress"
    if int(row.crystals_awarded or 0) > 0:
        return "rewarded"
    return "completed"


async def build_invitee_referral_status(
    session: AsyncSession,
    invitee_discord_id: int,
    *,
    inviter_profiles: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """Progress view when the current user joined via someone else's invite."""
    row = await session.get(GuildReferral, invitee_discord_id)
    if row is None:
        return None
    inviter_id = int(row.inviter_discord_id)
    profile = inviter_profiles.get(inviter_id) or {
        "id": str(inviter_id),
        "username": None,
        "global_name": None,
        "avatar_url": None,
    }
    display = profile.get("global_name") or profile.get("username")
    cd_uses = int(row.cd_uses or 0)
    first_pack_done = row.first_pack_rewarded_at is not None
    return {
        "inviter": profile,
        "inviter_display_name": display or f"User {inviter_id}",
        "joined_at": row.joined_at.isoformat() if row.joined_at else None,
        "cd_uses": cd_uses,
        "cd_uses_required": REFERRAL_CD_USES_REQUIRED,
        "first_pack_rewarded": first_pack_done,
        "invitee_crystals_awarded": int(row.invitee_crystals_awarded or 0),
        "status": referral_status_for_row(row),
        "guild_id": str(row.guild_id),
    }


async def build_referral_dashboard(
    session: AsyncSession,
    inviter_discord_id: int,
    *,
    invitee_profiles: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Summary + per-invitee rows for the website referrals tab."""
    rows = await session.execute(
        select(GuildReferral)
        .where(GuildReferral.inviter_discord_id == inviter_discord_id)
        .order_by(GuildReferral.joined_at.desc())
    )
    referrals = list(rows.scalars())
    rewards_earned = await count_inviter_rewards(session, inviter_discord_id)
    in_progress = sum(1 for r in referrals if r.completed_at is None)
    completed = len(referrals) - in_progress

    items: list[dict[str, Any]] = []
    for row in referrals:
        invitee_id = int(row.invitee_discord_id)
        profile = invitee_profiles.get(invitee_id) or {
            "id": str(invitee_id),
            "username": None,
            "global_name": None,
            "avatar_url": None,
        }
        display = profile.get("global_name") or profile.get("username")
        items.append(
            {
                "invitee": profile,
                "display_name": display or f"User {invitee_id}",
                "joined_at": row.joined_at.isoformat() if row.joined_at else None,
                "cd_uses": int(row.cd_uses or 0),
                "cd_uses_required": REFERRAL_CD_USES_REQUIRED,
                "status": referral_status_for_row(row),
                "crystals_awarded": int(row.crystals_awarded or 0),
                "invitee_crystals_awarded": int(row.invitee_crystals_awarded or 0),
                "first_pack_rewarded": row.first_pack_rewarded_at is not None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "guild_id": str(row.guild_id),
            }
        )

    cap = REFERRAL_MAX_REWARDS_PER_INVITER
    return {
        "rules": {
            "cd_uses_required": REFERRAL_CD_USES_REQUIRED,
            "crystal_reward": REFERRAL_CRYSTAL_REWARD,
            "first_pack_invitee_crystals": REFERRAL_FIRST_PACK_INVITEE_CRYSTALS,
            "first_pack_inviter_crystals": REFERRAL_FIRST_PACK_INVITER_CRYSTALS,
            "max_rewards": cap,
            "min_inviter_account_age_days": REFERRAL_MIN_INVITER_ACCOUNT_AGE_DAYS,
            "how_it_works": (
                "Share your personal invite link below. When a friend joins for the "
                "**first time** in the server (via your link) you both earn Crystals on "
                f"their **first** **`/cd`** pack — **{REFERRAL_FIRST_PACK_INVITEE_CRYSTALS}** "
                f"for them, **{REFERRAL_FIRST_PACK_INVITER_CRYSTALS}** for you. After they use "
                f"**`/cd`** **{REFERRAL_CD_USES_REQUIRED}** times total, you earn "
                f"**{REFERRAL_CRYSTAL_REWARD}** more Crystals (up to **{cap}** friends). "
                f"Your account must be at least {REFERRAL_MIN_INVITER_ACCOUNT_AGE_DAYS} days old."
            ),
        },
        "summary": {
            "total_invited": len(referrals),
            "in_progress": in_progress,
            "completed": completed,
            "rewards_earned": rewards_earned,
            "rewards_cap": cap,
            "rewards_remaining": max(0, cap - rewards_earned),
        },
        "referrals": items,
    }


async def count_inviter_rewards(
    session: AsyncSession,
    inviter_discord_id: int,
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(GuildReferral)
        .where(
            GuildReferral.inviter_discord_id == inviter_discord_id,
            GuildReferral.crystals_awarded > 0,
        )
    )
    return int(result.scalar_one() or 0)


def _account_age_days(account_created_at: datetime | None) -> float | None:
    if account_created_at is None:
        return None
    created = account_created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (datetime.now(UTC) - created.astimezone(UTC)).total_seconds() / 86400.0


def _inviter_account_is_established(account_created_at: datetime | None) -> bool:
    age = _account_age_days(account_created_at)
    if age is None:
        # Unknown account age — fail closed (don't credit suspicious inviters).
        return False
    return age >= REFERRAL_MIN_INVITER_ACCOUNT_AGE_DAYS


async def mark_first_guild_join(
    session: AsyncSession,
    *,
    guild_id: int,
    discord_user_id: int,
) -> bool:
    """
    Record the first time we see a user in this guild.

    Returns ``True`` only on their **first** join (not a leave/rejoin).
    """
    key = (guild_id, discord_user_id)
    existing = await session.get(GuildMemberSeen, key)
    if existing is not None:
        return False
    session.add(GuildMemberSeen(guild_id=guild_id, discord_user_id=discord_user_id))
    await session.flush()
    return True


async def reset_referral_test_state(
    session: AsyncSession,
    *,
    discord_user_id: int,
    guild_id: int | None = None,
) -> dict[str, int]:
    """Clear referral tracking so an invitee can be tested again.

    Removes their ``guild_referrals`` row (as invitee) and ``guild_member_seen``
    rows so the next join counts as a first join. When ``guild_id`` is set, only
    that guild's ``guild_member_seen`` row is removed; otherwise all guilds.
    """
    referral_deleted = 0
    row = await session.get(GuildReferral, discord_user_id)
    if row is not None:
        await session.delete(row)
        referral_deleted = 1

    seen_stmt = delete(GuildMemberSeen).where(
        GuildMemberSeen.discord_user_id == discord_user_id
    )
    if guild_id is not None:
        seen_stmt = seen_stmt.where(GuildMemberSeen.guild_id == guild_id)
    seen_result = await session.execute(seen_stmt)
    seen_deleted = int(seen_result.rowcount or 0)
    await session.flush()
    return {"referral_deleted": referral_deleted, "seen_deleted": seen_deleted}


async def seed_guild_members_seen(
    session: AsyncSession,
    guild: discord.Guild,
) -> int:
    """Mark every current member as already seen (run once per guild on startup)."""
    added = 0
    for member in guild.members:
        if member.bot:
            continue
        if await mark_first_guild_join(
            session, guild_id=guild.id, discord_user_id=member.id
        ):
            added += 1
    return added


async def register_referral_join(
    session: AsyncSession,
    *,
    inviter_discord_id: int,
    invitee_discord_id: int,
    guild_id: int,
    invitee_account_created_at: datetime | None,
    inviter_account_created_at: datetime | None = None,
    is_first_guild_join: bool,
    skip_inviter_age_check: bool = False,
) -> tuple[bool, str]:
    """Record a new referred member.

    Returns ``(created, reason)``. ``reason`` is ``ok`` on success or a short code
    explaining the silent rejection (``self_invite`` / ``rejoin`` /
    ``inviter_too_new`` / ``already_referred``) so callers can log it.

    Invitees may be any account age — real friends usually have older Discord
    accounts. Abuse is limited by first-join-only, personal invite attribution,
    and the ``/cd`` use threshold.
    """
    if inviter_discord_id == invitee_discord_id:
        return False, "self_invite"
    if not is_first_guild_join:
        return False, "rejoin"
    if not skip_inviter_age_check and not _inviter_account_is_established(
        inviter_account_created_at
    ):
        return False, "inviter_too_new"
    existing = await session.get(GuildReferral, invitee_discord_id)
    if existing is not None:
        return False, "already_referred"
    session.add(
        GuildReferral(
            invitee_discord_id=invitee_discord_id,
            inviter_discord_id=inviter_discord_id,
            guild_id=guild_id,
            cd_uses=0,
            crystals_awarded=0,
            invitee_crystals_awarded=0,
        )
    )
    await session.flush()
    return True, "ok"


async def _award_first_pack_rewards(
    session: AsyncSession,
    row: GuildReferral,
) -> ReferralFirstPackNotice | None:
    if row.first_pack_rewarded_at is not None:
        return None
    now = datetime.now(UTC)
    crystals_svc = CrystalsService()
    invitee_amount = REFERRAL_FIRST_PACK_INVITEE_CRYSTALS
    inviter_amount = REFERRAL_FIRST_PACK_INVITER_CRYSTALS
    await crystals_svc.try_credit(session, row.invitee_discord_id, invitee_amount)
    await crystals_svc.try_credit(session, row.inviter_discord_id, inviter_amount)
    row.invitee_crystals_awarded = int(row.invitee_crystals_awarded or 0) + invitee_amount
    row.first_pack_rewarded_at = now
    return ReferralFirstPackNotice(
        inviter_id=int(row.inviter_discord_id),
        invitee_id=int(row.invitee_discord_id),
        inviter_crystals=inviter_amount,
        invitee_crystals=invitee_amount,
    )


async def record_referral_cd_use(
    session_factory: async_sessionmaker[AsyncSession],
    invitee_discord_id: int,
) -> ReferralCdUseResult | None:
    """
    Count one successful ``cd`` for a referred member.

    On the first ``/cd``, grants Crystals to inviter and invitee. At the threshold,
    grants the inviter completion reward (up to the cap). Returns notices for DMs.
    """
    async with session_factory() as session:
        row = await session.get(GuildReferral, invitee_discord_id)
        if row is None or row.completed_at is not None:
            return None

        row.cd_uses = int(row.cd_uses or 0) + 1
        cd_uses = int(row.cd_uses)
        result = ReferralCdUseResult()

        if cd_uses == 1:
            result = ReferralCdUseResult(
                first_pack=await _award_first_pack_rewards(session, row),
            )

        if cd_uses < REFERRAL_CD_USES_REQUIRED:
            await session.commit()
            return result if (result.first_pack or result.threshold) else None

        now = datetime.now(UTC)
        row.completed_at = now
        rewards_so_far = await count_inviter_rewards(session, row.inviter_discord_id)
        crystals = 0
        if rewards_so_far < REFERRAL_MAX_REWARDS_PER_INVITER:
            crystals = REFERRAL_CRYSTAL_REWARD
            await CrystalsService().try_credit(session, row.inviter_discord_id, crystals)
            row.crystals_awarded = crystals
        else:
            row.crystals_awarded = 0

        await session.commit()

        if crystals > 0:
            result = ReferralCdUseResult(
                first_pack=result.first_pack,
                threshold=ReferralRewardNotice(
                    inviter_id=row.inviter_discord_id,
                    invitee_id=invitee_discord_id,
                    crystals=crystals,
                    rewards_used=rewards_so_far + 1,
                ),
            )
        return result if (result.first_pack or result.threshold) else None


async def notify_referral_first_pack(
    bot: discord.Client,
    notice: ReferralFirstPackNotice,
) -> None:
    inviter_msg = [
        f"You earned **{format_crystals(notice.inviter_crystals)}**!",
        (
            "A friend you invited opened their **first** **`/cd`** pack. "
            f"They also received **{format_crystals(notice.invitee_crystals)}**."
        ),
        (
            f"Invite **{REFERRAL_CD_USES_REQUIRED - 1}** more packs from them and you can earn "
            f"**{format_crystals(REFERRAL_CRYSTAL_REWARD)}** when they hit "
            f"**{REFERRAL_CD_USES_REQUIRED}** total."
        ),
    ]
    invitee_msg = [
        f"You earned **{format_crystals(notice.invitee_crystals)}**!",
        "Welcome bonus for your **first** **`/cd`** pack after joining via a friend's invite.",
        (
            f"Keep using **`/cd`** — your inviter earns "
            f"**{format_crystals(REFERRAL_CRYSTAL_REWARD)}** when you reach "
            f"**{REFERRAL_CD_USES_REQUIRED}** packs."
        ),
    ]
    inviter_discord = "\n\n".join(inviter_msg)
    schedule_notification(
        bot,
        user_id=int(notice.inviter_id),
        kind="referral",
        title="Referral reward",
        body=f"Friend's first /cd pack — you earned {format_crystals(notice.inviter_crystals)}",
        discord_pref=PREF_REFERRALS,
        discord_body=inviter_discord,
    )
    invitee_msg = [
        f"You earned **{format_crystals(notice.invitee_crystals)}**!",
        "Welcome bonus for your **first** **`/cd`** pack after joining via a friend's invite.",
        (
            f"Keep using **`/cd`** — your inviter earns "
            f"**{format_crystals(REFERRAL_CRYSTAL_REWARD)}** when you reach "
            f"**{REFERRAL_CD_USES_REQUIRED}** packs."
        ),
    ]
    try:
        invitee = await bot.fetch_user(notice.invitee_id)
        await invitee.send("\n\n".join(invitee_msg))
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass
    except discord.HTTPException:
        _LOG.exception("First-pack referral DM failed for invitee %s", notice.invitee_id)


async def notify_referral_reward(
    bot: discord.Client,
    notice: ReferralRewardNotice,
) -> None:
    remaining = max(0, notice.rewards_cap - notice.rewards_used)
    lines = [
        f"You earned **{format_crystals(notice.crystals)}**!",
        (
            "A friend you invited joined the server and used **`/cd`** (or **`ppcd`**) "
            f"{REFERRAL_CD_USES_REQUIRED} times."
        ),
        (
            f"Referral rewards used: **{notice.rewards_used}** / **{notice.rewards_cap}**."
        ),
    ]
    if remaining > 0:
        lines.append(
            f"You can earn **{format_crystals(REFERRAL_CRYSTAL_REWARD)}** "
            f"{remaining} more time{'s' if remaining != 1 else ''} "
            "by inviting friends who play."
        )
    else:
        lines.append("You've reached the maximum referral rewards — thanks for spreading the word!")
    schedule_notification(
        bot,
        user_id=int(notice.inviter_id),
        kind="referral",
        title="Referral milestone",
        body=f"You earned {format_crystals(notice.crystals)} from a referral milestone.",
        discord_pref=PREF_REFERRALS,
        discord_body="\n\n".join(lines),
    )
