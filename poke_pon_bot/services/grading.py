"""Card grading: RNG 1–10 weighted by global copy rarity for that printing."""

from __future__ import annotations

import math
import random
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.collection_visibility import user_instance_not_in_active_auction
from poke_pon_bot.services.crystals import CrystalsService, InsufficientCrystalsError, format_crystals
from poke_pon_bot.services.instance_public_id import normalize_public_id
from poke_pon_bot.services.weighted_rng import weighted_choice

GRADE_CRYSTAL_COST = 15
GRADE_MIN = 1
GRADE_MAX = 10

GRADE_LABELS: dict[int, str] = {
    10: "GEM MT",
    9: "MINT",
    8: "NM-MT",
    7: "NM",
    6: "EX-MT",
    5: "EX",
    4: "VG-EX",
    3: "VG",
    2: "GOOD",
    1: "PR",
}


@dataclass(frozen=True)
class CopyRarityIndex:
    """How early this copy was minted globally for the same catalog printing."""

    copy_index: int
    total_copies: int

    @property
    def uniqueness(self) -> float:
        """1.0 = first copy ever; 0.0 = newest when many exist."""
        if self.total_copies <= 1:
            return 1.0
        return 1.0 - (self.copy_index - 1) / (self.total_copies - 1)


@dataclass(frozen=True)
class GradeRollPreview:
    copy_index: CopyRarityIndex
    crystal_cost: int
    has_grade: bool
    grade: int | None
    grade_label: str | None
    graded_at: datetime | None


@dataclass(frozen=True)
class GradeRollOutcome:
    ok: bool
    error: str | None = None
    grade: int | None = None
    grade_label: str | None = None
    new_crystal_balance: int | None = None
    copy_index: CopyRarityIndex | None = None


def grade_label(grade: int) -> str:
    return GRADE_LABELS.get(grade, str(grade))


# Grades 7–10 earn +2% shop sell payout per point above 6 (max +8% at GEM MT).
GRADE_SELL_BONUS_THRESHOLD = 6
GRADE_SELL_BONUS_PER_POINT = 2
GRADE_SELL_BONUS_MAX_PERCENT = 8


def grade_sell_bonus_percent(grade: int | None) -> int:
    if grade is None or grade <= GRADE_SELL_BONUS_THRESHOLD:
        return 0
    return min(
        (int(grade) - GRADE_SELL_BONUS_THRESHOLD) * GRADE_SELL_BONUS_PER_POINT,
        GRADE_SELL_BONUS_MAX_PERCENT,
    )


def grade_sell_multiplier(grade: int | None) -> float:
    return 1.0 + grade_sell_bonus_percent(grade) / 100.0


def format_grade_slab_badge(grade: int | None) -> str:
    """Compact prestige suffix for list lines (trades, auctions, leaderboards)."""
    if grade is None:
        return ""
    return f" · 🏆 **{int(grade)}** {grade_label(int(grade))}"


def grading_fields_for_instance(inst: UserCardInstance) -> dict[str, int | str | None]:
    g = inst.grade
    if g is None:
        return {"grade": None, "grade_label": None}
    gi = int(g)
    return {"grade": gi, "grade_label": grade_label(gi)}


def roll_grade(*, copy_index: CopyRarityIndex, rng: random.Random | None = None) -> int:
    """Roll a grade 1–10; earlier global copies skew toward higher grades."""
    r = rng or random.Random(secrets.randbits(128))
    u = copy_index.uniqueness
    center = 3.2 + 6.3 * u
    spread = 1.35 + 0.55 * (1.0 - u)
    items: list[tuple[int, float]] = []
    for g in range(GRADE_MIN, GRADE_MAX + 1):
        dist = abs(g - center)
        w = math.exp(-(dist**2) / (2.0 * spread**2))
        items.append((g, max(w, 1e-6)))
    return weighted_choice(r, items)


async def global_copy_index(
    session: AsyncSession,
    *,
    card_id: int,
    obtained_at: datetime,
    instance_id: int,
) -> CopyRarityIndex:
    """1-based rank among all copies of this printing (earliest ``obtained_at`` = #1)."""
    key = (obtained_at, instance_id)
    if obtained_at.tzinfo is None:
        obtained_at = obtained_at.replace(tzinfo=UTC)
    else:
        obtained_at = obtained_at.astimezone(UTC)

    before = int(
        (
            await session.execute(
                select(func.count())
                .select_from(UserCardInstance)
                .where(
                    UserCardInstance.card_id == card_id,
                    or_(
                        UserCardInstance.obtained_at < obtained_at,
                        and_(
                            UserCardInstance.obtained_at == obtained_at,
                            UserCardInstance.id < instance_id,
                        ),
                    ),
                )
            )
        ).scalar()
        or 0
    )
    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(UserCardInstance)
                .where(UserCardInstance.card_id == card_id)
            )
        ).scalar()
        or 0
    )
    return CopyRarityIndex(copy_index=max(1, before + 1), total_copies=max(1, total))


async def build_grade_preview(
    session: AsyncSession,
    inst: UserCardInstance,
) -> GradeRollPreview:
    idx = await global_copy_index(
        session,
        card_id=inst.card_id,
        obtained_at=inst.obtained_at,
        instance_id=inst.id,
    )
    g = inst.grade
    return GradeRollPreview(
        copy_index=idx,
        crystal_cost=GRADE_CRYSTAL_COST,
        has_grade=g is not None,
        grade=g,
        grade_label=grade_label(g) if g is not None else None,
        graded_at=inst.graded_at,
    )


def grading_api_payload(preview: GradeRollPreview) -> dict:
    return {
        "grade": preview.grade,
        "grade_label": preview.grade_label,
        "graded_at": preview.graded_at.isoformat() if preview.graded_at else None,
        "crystal_cost": preview.crystal_cost,
        "copy_index": preview.copy_index.copy_index,
        "total_copies": preview.copy_index.total_copies,
        "uniqueness": round(preview.copy_index.uniqueness, 4),
        "can_roll": True,
        "can_remove": preview.has_grade,
    }


async def remove_grade(
    session: AsyncSession,
    *,
    discord_user_id: int,
    instance_id: int,
) -> str | None:
    """Clear grade. Returns error message or ``None`` on success."""
    inst = await session.get(UserCardInstance, instance_id)
    if inst is None or inst.discord_user_id != discord_user_id:
        return "Card not found in your collection."
    if inst.grade is None:
        return "This copy has no grade to remove."
    inst.grade = None
    inst.graded_at = None
    return None


async def roll_grade_for_instance(
    session: AsyncSession,
    crystals: CrystalsService,
    *,
    discord_user_id: int,
    instance_id: int,
) -> GradeRollOutcome:
    inst = await session.get(UserCardInstance, instance_id)
    if inst is None or inst.discord_user_id != discord_user_id:
        return GradeRollOutcome(ok=False, error="Card not found in your collection.")

    idx = await global_copy_index(
        session,
        card_id=inst.card_id,
        obtained_at=inst.obtained_at,
        instance_id=inst.id,
    )
    try:
        await crystals.try_debit(session, discord_user_id, GRADE_CRYSTAL_COST)
    except InsufficientCrystalsError:
        bal = await crystals.get_balance(session, discord_user_id)
        return GradeRollOutcome(
            ok=False,
            error=(
                f"Not enough Crystals — grading costs {format_crystals(GRADE_CRYSTAL_COST)} "
                f"(you have {format_crystals(bal)})."
            ),
        )

    rolled = roll_grade(copy_index=idx)
    inst.grade = rolled
    inst.graded_at = datetime.now(UTC)
    bal = await crystals.get_balance(session, discord_user_id)
    return GradeRollOutcome(
        ok=True,
        grade=rolled,
        grade_label=grade_label(rolled),
        new_crystal_balance=bal,
        copy_index=idx,
    )


async def load_owned_instance_for_grading(
    session: AsyncSession,
    *,
    discord_user_id: int,
    public_id: str,
) -> tuple[UserCardInstance, Card] | None:
    n = normalize_public_id(public_id)
    if n is None:
        return None
    row = (
        await session.execute(
            select(UserCardInstance, Card)
            .join(Card, UserCardInstance.card_id == Card.id)
            .where(
                UserCardInstance.discord_user_id == discord_user_id,
                UserCardInstance.public_id == n,
                user_instance_not_in_active_auction(),
            )
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]
