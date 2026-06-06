"""Seasonal set chase — featured set, community bar, personal completion rewards."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.set_chase import (
    SetChaseParticipant,
    SetChaseRewardClaim,
    SetChaseSeason,
)
from poke_pon_bot.services.cd_drop_themes import ResolvedCdDropTheme
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.wallet import WalletService

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SetProgress:
    owned_unique: int
    total_unique: int
    percent: float


@dataclass(frozen=True)
class SetChaseStatus:
    season: SetChaseSeason
    global_percent: float
    personal: SetProgress | None
    reward_claimed: bool
    reward_eligible: bool
    community_goal_reached: bool
    user_participated: bool
    user_community_reward_paid: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_set_code(code: str) -> str:
    return str(code or "").strip().lower()


def format_set_chase_cd_header(
    season: SetChaseSeason,
    *,
    chase_slot_indices: list[int],
    cards: list,
) -> str:
    """One-line status for ``/cd`` plus which slots used the chase-set roll."""
    set_label = season.set_name or season.set_code
    base = (
        f"🎯 **Set chase — {season.title}:** community "
        f"**{int(season.global_claims):,}/{int(season.global_target):,}** · "
        f"**{int(season.drop_boost_percent)}%** chance per slot from **{set_label}**"
    )
    picks: list[str] = []
    for idx in chase_slot_indices:
        if 0 <= idx < len(cards):
            c = cards[idx]
            picks.append(f"**#{idx + 1}** {c.name}")
    if picks:
        base += f" · Chase rolls: {', '.join(picks)}"
    return f"{base} · `/setchase`"


def progress_bar(percent: float, *, width: int = 12) -> str:
    pct = max(0.0, min(100.0, float(percent)))
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


async def get_active_season(session: AsyncSession) -> SetChaseSeason | None:
    now = _utc_now()
    try:
        row = (
            await session.execute(
                select(SetChaseSeason)
                .where(
                    SetChaseSeason.enabled.is_(True),
                    SetChaseSeason.starts_at <= now,
                    SetChaseSeason.ends_at > now,
                )
                .order_by(SetChaseSeason.starts_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    except SQLAlchemyError:
        _LOG.exception("Set chase lookup failed — treating as inactive")
        return None
    return row


async def resolve_set_chase_drop_theme(session: AsyncSession) -> ResolvedCdDropTheme | None:
    season = await get_active_season(session)
    if season is None or int(season.drop_boost_percent) <= 0:
        return None
    code = _normalize_set_code(season.set_code)
    label = season.set_name or season.set_code
    return ResolvedCdDropTheme(
        scope_label="set chase",
        kind="set",
        value=season.set_code,
        chance_percent=int(season.drop_boost_percent),
        set_codes=(code,),
        name_contains=None,
        display=f"**{season.title}** ({label})",
    )


async def set_catalog_size(session: AsyncSession, set_code: str) -> int:
    code = _normalize_set_code(set_code)
    count = await session.scalar(
        select(func.count())
        .select_from(Card)
        .where(func.lower(Card.set_code) == code)
    )
    return int(count or 0)


async def user_set_progress(
    session: AsyncSession, *, discord_user_id: int, set_code: str
) -> SetProgress:
    code = _normalize_set_code(set_code)
    total = await set_catalog_size(session, code)
    if total <= 0:
        return SetProgress(owned_unique=0, total_unique=0, percent=0.0)
    owned = await session.scalar(
        select(func.count(func.distinct(UserCardInstance.card_id)))
        .select_from(UserCardInstance)
        .join(Card, UserCardInstance.card_id == Card.id)
        .where(
            UserCardInstance.discord_user_id == int(discord_user_id),
            func.lower(Card.set_code) == code,
        )
    )
    owned_n = int(owned or 0)
    pct = (owned_n / total) * 100.0 if total else 0.0
    return SetProgress(owned_unique=owned_n, total_unique=total, percent=pct)


async def _get_participant(
    session: AsyncSession, *, season_id: int, discord_user_id: int
) -> SetChaseParticipant | None:
    return (
        await session.execute(
            select(SetChaseParticipant).where(
                SetChaseParticipant.season_id == int(season_id),
                SetChaseParticipant.discord_user_id == int(discord_user_id),
            )
        )
    ).scalar_one_or_none()


async def register_set_chase_participant(
    session: AsyncSession, *, season: SetChaseSeason, discord_user_id: int
) -> SetChaseParticipant:
    row = await _get_participant(
        session, season_id=int(season.id), discord_user_id=discord_user_id
    )
    if row is None:
        row = SetChaseParticipant(
            season_id=int(season.id),
            discord_user_id=int(discord_user_id),
            claim_count=1,
        )
        session.add(row)
    else:
        row.claim_count = int(row.claim_count or 0) + 1
    await session.flush()
    return row


async def trigger_community_participation_payout(
    session: AsyncSession, season: SetChaseSeason
) -> int:
    """Pay all registered participants once when the community bar fills."""
    if season.community_goal_reached_at is not None:
        return 0
    target = max(1, int(season.global_target or 1))
    if int(season.global_claims or 0) < target:
        return 0

    now = _utc_now()
    season.community_goal_reached_at = now
    amount = max(0, int(season.community_participation_crystals or 0))
    crystals = CrystalsService()
    rows = (
        await session.execute(
            select(SetChaseParticipant).where(
                SetChaseParticipant.season_id == int(season.id),
                SetChaseParticipant.community_reward_paid_at.is_(None),
            )
        )
    ).scalars().all()
    paid = 0
    for participant in rows:
        if amount > 0:
            await crystals.try_credit(session, int(participant.discord_user_id), amount)
        participant.community_reward_paid_at = now
        paid += 1
    await session.flush()
    if paid:
        _LOG.info(
            "Set chase community goal reached: season=%s participants=%s crystals=%s each",
            season.season_key,
            paid,
            amount,
        )
    return paid


async def reconcile_community_payout(session: AsyncSession) -> int:
    """Catch up if the goal was already met before payout ran (e.g. after deploy)."""
    season = await get_active_season(session)
    if season is None:
        return 0
    return await trigger_community_participation_payout(session, season)


async def dev_simulate_community_payout(
    session: AsyncSession,
    *,
    register_user_id: int | None = None,
) -> tuple[SetChaseSeason | None, int, str | None]:
    """Fill the community bar and pay all unpaid participants (developer testing)."""
    season = await get_active_season(session)
    if season is None:
        return None, 0, "There is no active set chase right now."
    if season.community_goal_reached_at is not None:
        return (
            season,
            0,
            "Community payout already ran for this season. Use `/dev set_chase reset` to test again.",
        )
    if register_user_id is not None:
        await register_set_chase_participant(
            session, season=season, discord_user_id=register_user_id
        )
    target = max(1, int(season.global_target or 1))
    season.global_claims = target
    paid = await trigger_community_participation_payout(session, season)
    await session.flush()
    return season, paid, None


async def dev_reset_community_payout(
    session: AsyncSession,
    *,
    reset_claims: bool = False,
) -> tuple[SetChaseSeason | None, dict[str, int], str | None]:
    """Clear community payout state so developers can re-test."""
    season = await get_active_season(session)
    if season is None:
        return None, {}, "There is no active set chase right now."
    participants = (
        await session.execute(
            select(SetChaseParticipant).where(
                SetChaseParticipant.season_id == int(season.id),
            )
        )
    ).scalars().all()
    unpaid_reset = 0
    for participant in participants:
        if participant.community_reward_paid_at is not None:
            participant.community_reward_paid_at = None
            unpaid_reset += 1
    season.community_goal_reached_at = None
    claims_before = int(season.global_claims or 0)
    if reset_claims:
        season.global_claims = 0
    await session.flush()
    return (
        season,
        {
            "participants_reset": unpaid_reset,
            "claims_before": claims_before,
            "claims_after": int(season.global_claims or 0),
        },
        None,
    )


async def reward_already_claimed(
    session: AsyncSession, *, season_id: int, discord_user_id: int
) -> bool:
    hit = await session.scalar(
        select(SetChaseRewardClaim.id)
        .where(
            SetChaseRewardClaim.season_id == int(season_id),
            SetChaseRewardClaim.discord_user_id == int(discord_user_id),
        )
        .limit(1)
    )
    return hit is not None


async def build_status(
    session: AsyncSession, *, discord_user_id: int | None = None
) -> SetChaseStatus | None:
    season = await get_active_season(session)
    if season is None:
        return None
    target = max(1, int(season.global_target or 1))
    global_pct = min(100.0, (int(season.global_claims) / target) * 100.0)
    personal = None
    claimed = False
    eligible = False
    user_participated = False
    user_community_reward_paid = False
    if discord_user_id is not None:
        personal = await user_set_progress(session, discord_user_id=discord_user_id, set_code=season.set_code)
        claimed = await reward_already_claimed(
            session, season_id=int(season.id), discord_user_id=discord_user_id
        )
        threshold = max(1, min(100, int(season.completion_threshold_pct or 80)))
        eligible = (
            not claimed
            and personal.total_unique > 0
            and personal.percent >= float(threshold)
        )
        participant = await _get_participant(
            session, season_id=int(season.id), discord_user_id=discord_user_id
        )
        if participant is not None:
            user_participated = True
            user_community_reward_paid = participant.community_reward_paid_at is not None
    return SetChaseStatus(
        season=season,
        global_percent=global_pct,
        personal=personal,
        reward_claimed=claimed,
        reward_eligible=eligible,
        community_goal_reached=season.community_goal_reached_at is not None,
        user_participated=user_participated,
        user_community_reward_paid=user_community_reward_paid,
    )


async def record_set_chase_claim(
    session: AsyncSession, *, discord_user_id: int, card: Card
) -> bool:
    """Track participation and increment the community counter for featured-set `/cd` claims."""
    season = await get_active_season(session)
    if season is None:
        return False
    if _normalize_set_code(card.set_code) != _normalize_set_code(season.set_code):
        return False
    await register_set_chase_participant(
        session, season=season, discord_user_id=discord_user_id
    )
    season.global_claims = int(season.global_claims or 0) + 1
    await trigger_community_participation_payout(session, season)
    await session.flush()
    return True


async def claim_personal_reward(
    session: AsyncSession,
    *,
    discord_user_id: int,
) -> tuple[SetChaseSeason | None, str | None]:
    status = await build_status(session, discord_user_id=discord_user_id)
    if status is None:
        return None, "There is no active set chase right now."
    season = status.season
    if status.reward_claimed:
        return season, "You already claimed this season's set chase reward."
    if status.personal is None or status.personal.total_unique <= 0:
        return season, "That set is not in the catalog yet."
    threshold = max(1, min(100, int(season.completion_threshold_pct or 80)))
    if status.personal.percent < float(threshold):
        return (
            season,
            f"Collect at least **{threshold}%** of **{season.set_name or season.set_code}** "
            f"({status.personal.owned_unique}/{status.personal.total_unique} unique) first.",
        )
    session.add(
        SetChaseRewardClaim(
            season_id=int(season.id),
            discord_user_id=int(discord_user_id),
        )
    )
    crystals = CrystalsService()
    wallet = WalletService()
    cr = max(0, int(season.reward_crystals or 0))
    pd = max(0, int(season.reward_pokedollars or 0))
    if cr:
        await crystals.try_credit(session, discord_user_id, cr)
    if pd:
        await wallet.try_credit(session, discord_user_id, pd)
    await session.flush()
    return season, None


def format_set_chase_embed(status: SetChaseStatus) -> str:
    s = status.season
    target = max(1, int(s.global_target or 1))
    claims = int(s.global_claims or 0)
    set_label = s.set_name or s.set_code
    lines = [
        f"🎯 **{s.title}**",
        f"Featured set: **{set_label}** (`{s.set_code}`)",
        "",
        "**Community progress**",
        f"`{progress_bar(status.global_percent)}` **{claims:,} / {target:,}** `/cd` claims",
    ]
    part_cr = max(0, int(s.community_participation_crystals or 0))
    if status.community_goal_reached:
        lines.append(
            f"✅ **Community goal complete!** Each participant received **{part_cr}** 💎."
        )
    else:
        lines.append(
            f"_{int(status.global_percent)}% toward the shared goal — "
            f"everyone who claims from this set earns **{part_cr}** 💎 when it fills._"
        )
    lines.extend(
        [
            "",
            f"**Drop boost:** **{int(s.drop_boost_percent)}%** of each `/cd` card can come from this set.",
        ]
    )
    if status.personal is not None and status.user_participated and part_cr > 0:
        if status.user_community_reward_paid:
            lines.append(f"✅ You received the **{part_cr}** 💎 community participation bonus.")
        elif not status.community_goal_reached:
            lines.append("You are registered for the community bonus when the bar fills.")
    if status.personal is not None:
        p = status.personal
        threshold = max(1, min(100, int(s.completion_threshold_pct or 80)))
        lines.extend(
            [
                "",
                "**Your binder**",
                f"`{progress_bar(p.percent)}` **{p.owned_unique}/{p.total_unique}** unique ({p.percent:.0f}%)",
                f"Reach **{threshold}%** for **{int(s.reward_crystals)}** 💎 + **₽{int(s.reward_pokedollars):,}**.",
            ]
        )
        if status.reward_claimed:
            lines.append("✅ Season reward claimed.")
        elif status.reward_eligible:
            lines.append("🎁 **Reward ready** — use `/setchase claim`.")
    lines.append("")
    lines.append(f"Ends <t:{int(s.ends_at.timestamp())}:R>.")
    return "\n".join(lines)


def season_to_public_json(status: SetChaseStatus, *, personal: dict | None = None) -> dict:
    s = status.season
    target = max(1, int(s.global_target or 1))
    payload = {
        "active": True,
        "title": s.title,
        "set_code": s.set_code,
        "set_name": s.set_name or s.set_code,
        "starts_at": s.starts_at.isoformat(),
        "ends_at": s.ends_at.isoformat(),
        "drop_boost_percent": int(s.drop_boost_percent),
        "completion_threshold_pct": int(s.completion_threshold_pct),
        "reward_crystals": int(s.reward_crystals),
        "reward_pokedollars": int(s.reward_pokedollars),
        "community_participation_crystals": int(s.community_participation_crystals or 0),
        "community_goal_reached": status.community_goal_reached,
        "global": {
            "claims": int(s.global_claims or 0),
            "target": target,
            "percent": round(status.global_percent, 2),
        },
    }
    if personal is not None:
        payload["personal"] = personal
        payload["user_participated"] = bool(personal.get("participated"))
        payload["user_community_reward_paid"] = bool(personal.get("community_reward_paid"))
    return payload


async def ensure_season_from_settings(
    session: AsyncSession,
    *,
    enabled: bool,
    set_code: str | None,
    title: str | None,
    starts_at: datetime | None,
    ends_at: datetime | None,
    global_target: int,
    drop_boost_percent: int,
    completion_threshold_pct: int,
    reward_crystals: int,
    reward_pokedollars: int,
    community_participation_crystals: int,
) -> SetChaseSeason | None:
    """Upsert the configured season row when SET_CHASE_* env is present."""
    if not enabled or not set_code or not starts_at or not ends_at:
        return None
    code = _normalize_set_code(set_code)
    if not code:
        return None
    if starts_at >= ends_at:
        _LOG.warning("Set chase ignored: starts_at must be before ends_at.")
        return None
    set_name_row = await session.scalar(
        select(Card.set_name)
        .where(func.lower(Card.set_code) == code)
        .limit(1)
    )
    set_name = str(set_name_row or set_code)
    season_key = f"{code}:{starts_at.date().isoformat()}"
    row = await session.scalar(
        select(SetChaseSeason).where(SetChaseSeason.season_key == season_key)
    )
    display_title = (title or f"{set_name} Set Chase").strip()
    if row is None:
        row = SetChaseSeason(
            season_key=season_key,
            title=display_title,
            set_code=set_code.strip(),
            set_name=set_name,
            starts_at=starts_at,
            ends_at=ends_at,
            global_target=max(1, int(global_target)),
            drop_boost_percent=max(0, min(100, int(drop_boost_percent))),
            completion_threshold_pct=max(1, min(100, int(completion_threshold_pct))),
            reward_crystals=max(0, int(reward_crystals)),
            reward_pokedollars=max(0, int(reward_pokedollars)),
            community_participation_crystals=max(0, int(community_participation_crystals)),
            enabled=True,
        )
        session.add(row)
    else:
        row.title = display_title
        row.set_code = set_code.strip()
        row.set_name = set_name
        row.starts_at = starts_at
        row.ends_at = ends_at
        row.global_target = max(1, int(global_target))
        row.drop_boost_percent = max(0, min(100, int(drop_boost_percent)))
        row.completion_threshold_pct = max(1, min(100, int(completion_threshold_pct)))
        row.reward_crystals = max(0, int(reward_crystals))
        row.reward_pokedollars = max(0, int(reward_pokedollars))
        row.community_participation_crystals = max(0, int(community_participation_crystals))
        row.enabled = True
    await session.flush()
    return row
