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
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.collection_visibility import user_instance_not_in_active_auction
from poke_pon_bot.services.crystals import CrystalsService, InsufficientCrystalsError, format_crystals
from poke_pon_bot.services.grade_enchantments import (
    enchantment_api_payload,
    enchantment_or_default,
    roll_enchantment,
)
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
    enchantment_code: str | None = None
    rarity_bump: int = 0
    printed_rarity_name: str | None = None
    effective_rarity_name: str | None = None


@dataclass(frozen=True)
class GradeRollOutcome:
    ok: bool
    error: str | None = None
    grade: int | None = None
    grade_label: str | None = None
    new_crystal_balance: int | None = None
    copy_index: CopyRarityIndex | None = None
    enchantment_code: str | None = None
    enchantment_name: str | None = None
    rarity_bump: int = 0
    printed_rarity_name: str | None = None
    effective_rarity_name: str | None = None


def grade_label(grade: int) -> str:
    return GRADE_LABELS.get(grade, str(grade))


# Grades 7–10 earn +2% shop sell payout per point above 6 (max +8% at GEM MT).
GRADE_SELL_BONUS_THRESHOLD = 6
GRADE_SELL_BONUS_PER_POINT = 2
GRADE_SELL_BONUS_MAX_PERCENT = 8

# High grades can step the copy up the rarity ladder. Low grades never step down.
GRADE_RARITY_BUMP: dict[int, int] = {
    7: 1,
    8: 1,
    9: 2,
    10: 3,
}
RARITY_SORT_MAX = 10


def grade_sell_bonus_percent(grade: int | None) -> int:
    if grade is None or grade <= GRADE_SELL_BONUS_THRESHOLD:
        return 0
    return min(
        (int(grade) - GRADE_SELL_BONUS_THRESHOLD) * GRADE_SELL_BONUS_PER_POINT,
        GRADE_SELL_BONUS_MAX_PERCENT,
    )


def grade_sell_multiplier(grade: int | None) -> float:
    return 1.0 + grade_sell_bonus_percent(grade) / 100.0


def grade_rarity_bump(grade: int | None) -> int:
    """How many rarity ladder steps this grade adds. Never negative."""
    if grade is None:
        return 0
    return max(0, int(GRADE_RARITY_BUMP.get(int(grade), 0)))


def effective_rarity_sort_order(
    printed_sort_order: int,
    grade: int | None,
    *,
    max_order: int = RARITY_SORT_MAX,
) -> int:
    base = max(0, int(printed_sort_order))
    bumped = base + grade_rarity_bump(grade)
    return min(max(base, bumped), max_order)


def apply_grade_rarity(
    printed: RarityClass | None,
    grade: int | None,
    ladder: list[RarityClass],
) -> RarityClass | None:
    """Return printed rarity, or a higher ladder rung after a strong grade.

    Bump counts named ladder steps (Uncommon +1 → Rare), never down.
    """
    if printed is None:
        return None
    bump = grade_rarity_bump(grade)
    if bump <= 0 or not ladder:
        return printed
    ordered = sorted(ladder, key=lambda r: (int(r.sort_order), str(getattr(r, "code", ""))))
    idx = 0
    printed_id = getattr(printed, "id", None)
    for i, rc in enumerate(ordered):
        if printed_id is not None and getattr(rc, "id", None) == printed_id:
            idx = i
            break
        if int(rc.sort_order) <= int(printed.sort_order):
            idx = i
    chosen = ordered[min(idx + bump, len(ordered) - 1)]
    if int(chosen.sort_order) < int(printed.sort_order):
        return printed
    return chosen


async def load_rarity_ladder(session: AsyncSession) -> list[RarityClass]:
    rows = (
        await session.execute(select(RarityClass).order_by(RarityClass.sort_order))
    ).scalars().all()
    return list(rows)


async def rarity_for_graded_instance(
    session: AsyncSession,
    card: Card,
    inst: UserCardInstance,
    *,
    printed: RarityClass | None = None,
) -> RarityClass | None:
    if printed is None:
        printed = await session.get(RarityClass, card.rarity_class_id)
    ladder = await load_rarity_ladder(session)
    return apply_grade_rarity(printed, inst.grade, ladder)


async def rarity_name_pair(
    session: AsyncSession,
    card: Card,
    inst: UserCardInstance,
    *,
    printed: RarityClass | None = None,
) -> tuple[str | None, str | None]:
    """Printed display name, then graded/effective display name."""
    if printed is None:
        printed = await session.get(RarityClass, card.rarity_class_id)
    effective = await rarity_for_graded_instance(session, card, inst, printed=printed)
    printed_name = printed.display_name if printed is not None else (card.tcg_rarity or None)
    effective_name = effective.display_name if effective is not None else printed_name
    return printed_name, effective_name


def format_grade_slab_badge(grade: int | None, *, enchantment_code: str | None = None) -> str:
    """Compact prestige suffix for list lines (trades, auctions, leaderboards)."""
    if grade is None:
        return ""
    extra = ""
    if enchantment_code:
        ench = enchantment_or_default(enchantment_code)
        extra = f" · {ench.name}"
    return f" · 🏆 **{int(grade)}** {grade_label(int(grade))}{extra}"


def grading_fields_for_instance(inst: UserCardInstance) -> dict[str, int | str | None]:
    g = inst.grade
    if g is None:
        return {"grade": None, "grade_label": None, "enchantment": None}
    gi = int(g)
    code = getattr(inst, "grade_enchantment", None)
    return {
        "grade": gi,
        "grade_label": grade_label(gi),
        "enchantment": enchantment_api_payload(code, graded=True),
    }


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
    printed_name = None
    effective_name = None
    card = await session.get(Card, inst.card_id)
    if card is not None:
        printed_name, effective_name = await rarity_name_pair(session, card, inst)
    return GradeRollPreview(
        copy_index=idx,
        crystal_cost=GRADE_CRYSTAL_COST,
        has_grade=g is not None,
        grade=g,
        grade_label=grade_label(g) if g is not None else None,
        graded_at=inst.graded_at,
        enchantment_code=getattr(inst, "grade_enchantment", None) if g is not None else None,
        rarity_bump=grade_rarity_bump(g),
        printed_rarity_name=printed_name,
        effective_rarity_name=effective_name,
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
        "enchantment": enchantment_api_payload(
            preview.enchantment_code, graded=preview.has_grade
        ),
        "rarity_bump": preview.rarity_bump,
        "printed_rarity_name": preview.printed_rarity_name,
        "effective_rarity_name": preview.effective_rarity_name,
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
    inst.grade_enchantment = None
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
    ench = roll_enchantment()
    inst.grade = rolled
    inst.graded_at = datetime.now(UTC)
    inst.grade_enchantment = ench.code
    bump = grade_rarity_bump(rolled)
    printed_name = None
    effective_name = None
    card = await session.get(Card, inst.card_id)
    if card is not None:
        printed = await session.get(RarityClass, card.rarity_class_id)
        effective = await rarity_for_graded_instance(
            session, card, inst, printed=printed
        )
        printed_name = printed.display_name if printed is not None else None
        effective_name = effective.display_name if effective is not None else printed_name
    bal = await crystals.get_balance(session, discord_user_id)
    return GradeRollOutcome(
        ok=True,
        grade=rolled,
        grade_label=grade_label(rolled),
        new_crystal_balance=bal,
        copy_index=idx,
        enchantment_code=ench.code,
        enchantment_name=ench.name,
        rarity_bump=bump,
        printed_rarity_name=printed_name,
        effective_rarity_name=effective_name,
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
