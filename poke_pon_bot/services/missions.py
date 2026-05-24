"""Daily and weekly crystal missions — roll targets, track progress, claim rewards."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.mission import UserMission
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.crystals import CrystalsService

PeriodType = Literal["daily", "weekly"]
MissionKind = Literal["drop_uses", "claim_set", "duel_wins", "obtain_card"]

DAILY_SLOTS = 3
WEEKLY_SLOTS = 1

DROP_USES_RANGE = (6, 10)
DUEL_WINS_RANGE = (45, 55)
WEEKLY_RARITY_MIN_SORT = 7
MIN_CARDS_PER_SET = 15

@dataclass(frozen=True)
class PackMissionAlert:
    """A guild member whose active mission matches a card in this drop."""

    user_id: int
    text: str


@dataclass(frozen=True)
class MissionProgressNotice:
    """User-visible progress bump (e.g. after claiming a drop card)."""

    mission: UserMission
    previous_progress: int
    completed: bool


def daily_period_key(d: date | None = None) -> str:
    return (d or datetime.now(UTC).date()).isoformat()


def weekly_period_key(d: date | None = None) -> str:
    d = d or datetime.now(UTC).date()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _reward_drop_uses(target: int) -> int:
    """Middle tier — scales slightly with a higher drop count."""
    return 3 + max(0, target - 6) // 2


def _reward_duel_wins(_target: int) -> int:
    """Lowest tier — duels are the easiest to grind."""
    return 3


def _reward_claim_set() -> int:
    return 5


def _reward_obtain_card(rarity_sort: int) -> int:
    """Weekly — highest tier, still modest vs old values."""
    if rarity_sort >= 9:
        return 14
    if rarity_sort >= 8:
        return 12
    return 10


class MissionService:
    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random(secrets.randbits(128))

    async def list_active_missions(
        self, session: AsyncSession, discord_user_id: int
    ) -> list[UserMission]:
        await self._ensure_current_periods(session, discord_user_id)
        day = daily_period_key()
        week = weekly_period_key()
        rows = (
            await session.execute(
                select(UserMission)
                .where(
                    UserMission.discord_user_id == discord_user_id,
                    (
                        (UserMission.period_type == "daily")
                        & (UserMission.period_key == day)
                    )
                    | (
                        (UserMission.period_type == "weekly")
                        & (UserMission.period_key == week)
                    ),
                )
                .order_by(
                    UserMission.period_type,
                    UserMission.slot,
                )
            )
        ).scalars().all()
        return list(rows)

    async def _ensure_current_periods(
        self, session: AsyncSession, discord_user_id: int
    ) -> None:
        day = daily_period_key()
        week = weekly_period_key()
        existing = (
            await session.execute(
                select(UserMission.period_type, UserMission.period_key)
                .where(UserMission.discord_user_id == discord_user_id)
                .distinct()
            )
        ).all()
        have_daily = any(p == "daily" and k == day for p, k in existing)
        have_weekly = any(p == "weekly" and k == week for p, k in existing)
        if not have_daily:
            await self._roll_daily(session, discord_user_id, day)
        if not have_weekly:
            await self._roll_weekly(session, discord_user_id, week)

    async def _roll_daily(
        self, session: AsyncSession, discord_user_id: int, period_key: str
    ) -> None:
        drop_target = self._rng.randint(*DROP_USES_RANGE)
        duel_target = self._rng.randint(*DUEL_WINS_RANGE)
        set_code, set_name = await self._pick_random_set(session)

        missions = [
            UserMission(
                discord_user_id=discord_user_id,
                period_type="daily",
                period_key=period_key,
                slot=0,
                kind="drop_uses",
                target=drop_target,
                progress=0,
                reward_crystals=_reward_drop_uses(drop_target),
            ),
            UserMission(
                discord_user_id=discord_user_id,
                period_type="daily",
                period_key=period_key,
                slot=1,
                kind="claim_set",
                target=1,
                progress=0,
                set_code=set_code,
                set_name=set_name,
                reward_crystals=_reward_claim_set(),
            ),
            UserMission(
                discord_user_id=discord_user_id,
                period_type="daily",
                period_key=period_key,
                slot=2,
                kind="duel_wins",
                target=duel_target,
                progress=0,
                reward_crystals=_reward_duel_wins(duel_target),
            ),
        ]
        session.add_all(missions)
        await session.flush()

    async def _roll_weekly(
        self, session: AsyncSession, discord_user_id: int, period_key: str
    ) -> None:
        card = await self._pick_weekly_card(session)
        if card is None:
            set_code, set_name = await self._pick_random_set(session)
            mission = UserMission(
                discord_user_id=discord_user_id,
                period_type="weekly",
                period_key=period_key,
                slot=0,
                kind="claim_set",
                target=1,
                progress=0,
                set_code=set_code,
                set_name=set_name,
                reward_crystals=8,
            )
        else:
            rarity_sort = await self._card_rarity_sort(session, card)
            mission = UserMission(
                discord_user_id=discord_user_id,
                period_type="weekly",
                period_key=period_key,
                slot=0,
                kind="obtain_card",
                target=1,
                progress=0,
                card_id=card.id,
                card_name=card.name,
                set_code=card.set_code,
                set_name=card.set_name,
                reward_crystals=_reward_obtain_card(rarity_sort),
            )
        session.add(mission)
        await session.flush()

    async def _pick_random_set(self, session: AsyncSession) -> tuple[str, str]:
        row = (
            await session.execute(
                select(Card.set_code, Card.set_name, func.count(Card.id).label("n"))
                .group_by(Card.set_code, Card.set_name)
                .having(func.count(Card.id) >= MIN_CARDS_PER_SET)
                .order_by(func.random())
                .limit(1)
            )
        ).first()
        if row is None:
            row = (
                await session.execute(
                    select(Card.set_code, Card.set_name)
                    .distinct()
                    .order_by(func.random())
                    .limit(1)
                )
            ).first()
        if row is None:
            return "unknown", "Unknown set"
        return str(row[0]), str(row[1])

    async def _pick_weekly_card(self, session: AsyncSession) -> Card | None:
        return (
            await session.execute(
                select(Card)
                .join(RarityClass, Card.rarity_class_id == RarityClass.id)
                .where(RarityClass.sort_order >= WEEKLY_RARITY_MIN_SORT)
                .order_by(func.random())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _card_rarity_sort(self, session: AsyncSession, card: Card) -> int:
        rc = await session.get(RarityClass, card.rarity_class_id)
        return int(rc.sort_order) if rc is not None else 1

    async def record_drop_use(
        self, session: AsyncSession, discord_user_id: int
    ) -> list[MissionProgressNotice]:
        await self._ensure_current_periods(session, discord_user_id)
        return await self._bump_kind(
            session,
            discord_user_id,
            kinds=("drop_uses",),
            amount=1,
        )

    async def record_drop_claim(
        self, session: AsyncSession, discord_user_id: int, card: Card
    ) -> list[MissionProgressNotice]:
        await self._ensure_current_periods(session, discord_user_id)
        missions = await self.list_active_missions(session, discord_user_id)
        notices: list[MissionProgressNotice] = []
        for m in missions:
            if m.claimed_at is not None:
                continue
            if m.kind == "claim_set" and m.set_code and card.set_code == m.set_code:
                n = await self._bump_one(session, m, amount=1)
                if n is not None:
                    notices.append(n)
            elif m.kind == "obtain_card" and m.card_id == card.id:
                n = await self._bump_one(session, m, amount=1)
                if n is not None:
                    notices.append(n)
        return notices

    async def record_duel_win(
        self, session: AsyncSession, discord_user_id: int
    ) -> list[MissionProgressNotice]:
        await self._ensure_current_periods(session, discord_user_id)
        return await self._bump_kind(
            session,
            discord_user_id,
            kinds=("duel_wins",),
            amount=1,
        )

    async def _bump_kind(
        self,
        session: AsyncSession,
        discord_user_id: int,
        *,
        kinds: tuple[str, ...],
        amount: int,
    ) -> list[MissionProgressNotice]:
        day = daily_period_key()
        week = weekly_period_key()
        rows = (
            await session.execute(
                select(UserMission).where(
                    UserMission.discord_user_id == discord_user_id,
                    UserMission.kind.in_(kinds),
                    UserMission.claimed_at.is_(None),
                    (
                        (UserMission.period_type == "daily")
                        & (UserMission.period_key == day)
                    )
                    | (
                        (UserMission.period_type == "weekly")
                        & (UserMission.period_key == week)
                    ),
                )
            )
        ).scalars().all()
        notices: list[MissionProgressNotice] = []
        for m in rows:
            n = await self._bump_one(session, m, amount=amount)
            if n is not None:
                notices.append(n)
        return notices

    async def _bump_one(
        self,
        session: AsyncSession,
        mission: UserMission,
        *,
        amount: int,
    ) -> MissionProgressNotice | None:
        if mission.claimed_at is not None:
            return None
        prev = mission.progress
        if prev >= mission.target:
            return None
        mission.progress = min(mission.target, prev + amount)
        if mission.progress == prev:
            return None
        completed = mission.progress >= mission.target
        return MissionProgressNotice(
            mission=mission,
            previous_progress=prev,
            completed=completed,
        )

    async def claim_mission(
        self,
        session: AsyncSession,
        crystals: CrystalsService,
        *,
        discord_user_id: int,
        mission_id: int,
    ) -> tuple[UserMission | None, str | None]:
        mission = await session.get(UserMission, mission_id)
        if mission is None or mission.discord_user_id != discord_user_id:
            return None, "Mission not found."
        day = daily_period_key()
        week = weekly_period_key()
        if mission.period_type == "daily" and mission.period_key != day:
            return None, "That daily mission has expired."
        if mission.period_type == "weekly" and mission.period_key != week:
            return None, "That weekly mission has expired."
        if mission.claimed_at is not None:
            return None, "You already claimed this mission."
        if mission.progress < mission.target:
            return None, "Mission not complete yet."
        mission.claimed_at = datetime.now(UTC)
        await crystals.try_credit(
            session, discord_user_id, mission.reward_crystals
        )
        return mission, None

    def describe_mission(self, m: UserMission) -> str:
        prog = f"**{min(m.progress, m.target)}/{m.target}**"
        if m.kind == "drop_uses":
            return f"Use **`/cd`** (card drop) {prog} times"
        if m.kind == "claim_set":
            label = m.set_name or m.set_code or "a set"
            return f"Claim a card from **{label}** via **`/cd`** drop {prog}"
        if m.kind == "duel_wins":
            return f"Win **{m.target}** duels (PvP or wild) {prog}"
        if m.kind == "obtain_card":
            name = m.card_name or "a rare card"
            return f"Claim **{name}** from a **`/cd`** drop {prog}"
        return f"Unknown mission {prog}"

    def status_line(self, m: UserMission) -> str:
        if m.claimed_at is not None:
            return "✅ Claimed"
        if m.progress >= m.target:
            return "🎁 Ready to claim"
        return "⏳ In progress"

    def format_progress_notice(self, notice: MissionProgressNotice) -> str:
        m = notice.mission
        label = self.describe_mission(m)
        if notice.completed:
            return (
                f"⭐ **Mission complete!** {label}\n"
                f"Use **`/missions`** to claim **{m.reward_crystals}** 💎."
            )
        return (
            f"⭐ **Mission progress:** {label}\n"
            f"Progress: **{m.progress}/{m.target}**."
        )

    async def find_pack_mission_alerts(
        self,
        session: AsyncSession,
        cards: list[Card],
        *,
        exclude_user_ids: set[int] | None = None,
    ) -> list[PackMissionAlert]:
        """Users with an incomplete claim-from-drop mission matching this pack."""
        if not cards:
            return []
        card_ids = [c.id for c in cards]
        set_codes = list({c.set_code for c in cards if c.set_code})
        day = daily_period_key()
        week = weekly_period_key()
        period_ok = or_(
            and_(UserMission.period_type == "daily", UserMission.period_key == day),
            and_(UserMission.period_type == "weekly", UserMission.period_key == week),
        )
        kind_clauses = [
            and_(
                UserMission.kind == "obtain_card",
                UserMission.card_id.in_(card_ids),
            ),
        ]
        if set_codes:
            kind_clauses.append(
                and_(
                    UserMission.kind == "claim_set",
                    UserMission.set_code.in_(set_codes),
                )
            )
        kind_ok = kind_clauses[0] if len(kind_clauses) == 1 else or_(*kind_clauses)
        rows = (
            await session.execute(
                select(UserMission).where(
                    UserMission.claimed_at.is_(None),
                    UserMission.progress < UserMission.target,
                    period_ok,
                    kind_ok,
                )
            )
        ).scalars().all()
        pack_set_codes = {c.set_code for c in cards}
        pack_card_ids = set(card_ids)
        cards_by_set: dict[str, list[Card]] = {}
        for c in cards:
            if c.set_code:
                cards_by_set.setdefault(c.set_code, []).append(c)
        by_user: dict[int, list[str]] = {}
        skip = exclude_user_ids or set()
        for m in rows:
            uid = int(m.discord_user_id)
            if uid in skip:
                continue
            if m.kind == "claim_set":
                if not m.set_code or m.set_code not in pack_set_codes:
                    continue
                matches = cards_by_set.get(m.set_code, [])
                if not matches:
                    continue
                set_label = m.set_name or m.set_code
                if len(matches) == 1:
                    card = matches[0]
                    card_label = f"**{card.name}** (**{set_label}**)"
                    verb = "is"
                else:
                    names = ", ".join(f"**{c.name}**" for c in matches)
                    card_label = f"{names} (**{set_label}**)"
                    verb = "are"
                by_user.setdefault(uid, []).append(
                    f"{card_label} {verb} on your **daily** mission — claim "
                    f"{'one' if len(matches) > 1 else 'it'} from this drop!"
                )
            elif m.kind == "obtain_card":
                if m.card_id is None or int(m.card_id) not in pack_card_ids:
                    continue
                name = m.card_name or "that card"
                match = next((c for c in cards if c.id == m.card_id), None)
                set_hint = ""
                if match is not None:
                    sl = (match.set_name or match.set_code or "").strip()
                    if sl:
                        set_hint = f" (**{sl}**)"
                by_user.setdefault(uid, []).append(
                    f"**{name}**{set_hint} is on your **weekly** mission — claim it from this drop!"
                )
        return [
            PackMissionAlert(user_id=uid, text=" ".join(parts))
            for uid, parts in by_user.items()
        ]
