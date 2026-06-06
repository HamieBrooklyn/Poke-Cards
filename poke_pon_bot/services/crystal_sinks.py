"""Optional crystal spends — rerolls, auction spotlight, cosmetic leaderboard frames."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.auction import CardAuction
from poke_pon_bot.models.user_web_preferences import UserWebPreferences
from poke_pon_bot.services.crystals import CrystalsService, InsufficientCrystalsError, format_crystals
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.services.event_scheduler import resolve_spotlight_crystal_cost

# --- Costs (tune via env later if needed) ---
CD_REROLL_CRYSTAL_COST = 8
AUCTION_SPOTLIGHT_CRYSTAL_COST = 12
AUCTION_SPOTLIGHT_HOURS = 24

LEADERBOARD_FRAME_CATALOG: dict[str, dict[str, Any]] = {
    "bronze": {"label": "Bronze", "cost": 10, "group": "metal"},
    "silver": {"label": "Silver", "cost": 20, "group": "metal"},
    "gold": {"label": "Gold", "cost": 35, "group": "metal"},
    "emerald": {"label": "Emerald", "cost": 18, "group": "gem"},
    "sapphire": {"label": "Sapphire", "cost": 22, "group": "gem"},
    "ruby": {"label": "Ruby", "cost": 25, "group": "gem"},
    "sunset": {"label": "Sunset", "cost": 28, "group": "effect"},
    "midnight": {"label": "Midnight", "cost": 30, "group": "effect"},
    "neon": {"label": "Neon Pulse", "cost": 32, "group": "effect"},
    "aurora": {"label": "Aurora", "cost": 40, "group": "effect"},
    "cosmic": {"label": "Cosmic", "cost": 45, "group": "effect"},
}


def crystal_sinks_catalog_payload() -> dict[str, Any]:
    return {
        "cd_reroll_cost": CD_REROLL_CRYSTAL_COST,
        "auction_spotlight_cost": AUCTION_SPOTLIGHT_CRYSTAL_COST,
        "auction_spotlight_hours": AUCTION_SPOTLIGHT_HOURS,
        "leaderboard_frames": [
            {"id": fid, **meta} for fid, meta in LEADERBOARD_FRAME_CATALOG.items()
        ],
    }


def parse_yes_no_spotlight(raw: str | None) -> bool:
    if not raw or not str(raw).strip():
        return False
    v = str(raw).strip().lower()
    return v in {"yes", "y", "true", "1", "on", "spotlight", "✓"}


def _parse_unlocked_frames(row: UserWebPreferences) -> set[str]:
    raw = getattr(row, "unlocked_leaderboard_frames", None)
    if not raw:
        return set()
    if isinstance(raw, list):
        return {str(x) for x in raw if str(x) in LEADERBOARD_FRAME_CATALOG}
    return set()


def _set_unlocked_frames(row: UserWebPreferences, frames: set[str]) -> None:
    row.unlocked_leaderboard_frames = sorted(frames)


def serialize_cosmetics(row: UserWebPreferences | None) -> dict[str, Any]:
    if row is None:
        return {
            "leaderboard_frame": None,
            "unlocked_leaderboard_frames": [],
        }
    equipped = getattr(row, "leaderboard_frame", None)
    if equipped and equipped not in LEADERBOARD_FRAME_CATALOG:
        equipped = None
    unlocked = sorted(_parse_unlocked_frames(row))
    return {
        "leaderboard_frame": equipped,
        "unlocked_leaderboard_frames": unlocked,
    }


def auction_spotlight_active(auc: CardAuction, *, now: datetime | None = None) -> bool:
    until = getattr(auc, "spotlight_until", None)
    if until is None:
        return False
    ref = now or datetime.now(UTC)
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until > ref


def spotlight_until_from_now(*, now: datetime | None = None) -> datetime:
    ref = now or datetime.now(UTC)
    return ref + timedelta(hours=AUCTION_SPOTLIGHT_HOURS)


async def debit_for_auction_spotlight(
    session: AsyncSession,
    crystals: CrystalsService,
    *,
    seller_discord_id: int,
    cost: int | None = None,
) -> str | None:
    """Debit spotlight cost. Returns error message or None."""
    charge = AUCTION_SPOTLIGHT_CRYSTAL_COST if cost is None else int(cost)
    if charge <= 0:
        return None
    try:
        await crystals.try_debit(session, int(seller_discord_id), charge)
    except InsufficientCrystalsError:
        bal = await crystals.get_balance(session, int(seller_discord_id))
        return (
            f"Spotlight costs {format_crystals(charge)} "
            f"(you have {format_crystals(bal)})."
        )
    return None


@dataclass(frozen=True)
class SpotlightOutcome:
    ok: bool
    error: str | None = None
    auction_id: int | None = None
    spotlight_until: datetime | None = None
    new_crystal_balance: int | None = None


async def apply_spotlight_to_auction(
    session: AsyncSession,
    crystals: CrystalsService,
    *,
    seller_discord_id: int,
    auction_id: int,
) -> SpotlightOutcome:
    """Feature an active listing in search for 24h (seller only, costs crystals)."""
    from poke_pon_bot.models.auction import AUCTION_STATUS_ACTIVE
    from poke_pon_bot.services.auction_runtime import format_auction_time_remaining

    auc = await session.get(CardAuction, int(auction_id))
    if auc is None or auc.status != AUCTION_STATUS_ACTIVE:
        return SpotlightOutcome(ok=False, error="No active auction with that listing number.")
    if int(auc.seller_discord_id) != int(seller_discord_id):
        return SpotlightOutcome(ok=False, error="Only the seller can spotlight this listing.")
    if auction_spotlight_active(auc):
        until = auc.spotlight_until
        assert until is not None
        left = format_auction_time_remaining(until)
        return SpotlightOutcome(
            ok=False,
            error=f"Spotlight is already active on this listing (**{left}**).",
        )

    spot_err = await debit_for_auction_spotlight(
        session,
        crystals,
        seller_discord_id=seller_discord_id,
        cost=await resolve_spotlight_crystal_cost(session, auction_instance_id=int(auc.instance_id)),
    )
    if spot_err is not None:
        return SpotlightOutcome(ok=False, error=spot_err)

    until = spotlight_until_from_now()
    auc.spotlight_until = until
    bal = await crystals.get_balance(session, int(seller_discord_id))
    return SpotlightOutcome(
        ok=True,
        auction_id=int(auc.id),
        spotlight_until=until,
        new_crystal_balance=bal,
    )


async def reroll_cd_pack_slot(
    session: AsyncSession,
    *,
    guild_id: int | None,
):
    """Roll one replacement card using the same `/cd` luck + theme rules."""
    return await DropService().reroll_cd_slot(session, guild_id=guild_id)


@dataclass(frozen=True)
class FrameUnlockOutcome:
    ok: bool
    error: str | None = None
    frame_id: str | None = None
    new_crystal_balance: int | None = None
    unlocked_frames: list[str] | None = None


async def unlock_leaderboard_frame(
    session: AsyncSession,
    crystals: CrystalsService,
    *,
    discord_user_id: int,
    frame_id: str,
) -> FrameUnlockOutcome:
    fid = (frame_id or "").strip().lower()
    meta = LEADERBOARD_FRAME_CATALOG.get(fid)
    if meta is None:
        return FrameUnlockOutcome(ok=False, error="Unknown frame.")
    row = await _get_prefs_row(session, discord_user_id)
    unlocked = _parse_unlocked_frames(row)
    if fid in unlocked:
        return FrameUnlockOutcome(ok=False, error="You already own that frame.")
    cost = int(meta["cost"])
    try:
        await crystals.try_debit(session, discord_user_id, cost)
    except InsufficientCrystalsError:
        bal = await crystals.get_balance(session, discord_user_id)
        return FrameUnlockOutcome(
            ok=False,
            error=f"Costs {format_crystals(cost)} (you have {format_crystals(bal)}).",
        )
    unlocked.add(fid)
    _set_unlocked_frames(row, unlocked)
    bal = await crystals.get_balance(session, discord_user_id)
    return FrameUnlockOutcome(
        ok=True,
        frame_id=fid,
        new_crystal_balance=bal,
        unlocked_frames=sorted(unlocked),
    )


async def equip_leaderboard_frame(
    session: AsyncSession,
    *,
    discord_user_id: int,
    frame_id: str | None,
) -> str | None:
    row = await _get_prefs_row(session, discord_user_id)
    if frame_id is None or frame_id == "":
        row.leaderboard_frame = None
        return None
    fid = str(frame_id).strip().lower()
    if fid not in LEADERBOARD_FRAME_CATALOG:
        return "Unknown frame."
    unlocked = _parse_unlocked_frames(row)
    if fid not in unlocked:
        return "Unlock that frame first."
    row.leaderboard_frame = fid
    return None


async def load_equipped_leaderboard_frames(
    session: AsyncSession,
    user_ids: set[int],
) -> dict[int, str | None]:
    if not user_ids:
        return {}
    from sqlalchemy import select

    rows = (
        await session.execute(
            select(UserWebPreferences).where(
                UserWebPreferences.discord_user_id.in_(user_ids)
            )
        )
    ).scalars()
    out: dict[int, str | None] = {int(uid): None for uid in user_ids}
    for row in rows:
        fid = getattr(row, "leaderboard_frame", None)
        if fid and fid in LEADERBOARD_FRAME_CATALOG:
            out[int(row.discord_user_id)] = fid
    return out


async def _get_prefs_row(session: AsyncSession, discord_user_id: int) -> UserWebPreferences:
    from poke_pon_bot.services.web_preferences import get_or_create_web_preferences

    return await get_or_create_web_preferences(session, discord_user_id)
