"""Pokedollars balance and daily claim (UTC day)."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.pokedollars import UserPokedollars
from poke_pon_bot.models.topgg_processed_vote import TopggProcessedVote

CURRENCY_SYMBOL = "₽"
CURRENCY_NAME = "Pokedollars"
DAILY_CLAIM_MIN = 100
DAILY_CLAIM_MAX = 200
VOTE_CLAIM_MIN = 100
VOTE_CLAIM_MAX = 200


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _same_topgg_vote_slice(a: datetime | None, b: datetime) -> bool:
    if a is None:
        return False
    return abs((_utc(a) - _utc(b)).total_seconds()) < 2.0


class InsufficientPokedollarsError(Exception):
    """Balance too low for the debit amount."""


class AlreadyClaimedTodayError(Exception):
    """User already used their daily claim for the current UTC calendar day."""


class AlreadyClaimedVoteRewardError(Exception):
    """User already claimed their reward for this Top.gg vote."""

    def __init__(self, *, retry_after_seconds: float) -> None:
        super().__init__("vote reward already claimed")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class DailyClaimResult:
    amount: int
    new_balance: int
    #: Crystals also credited (0 when the slice was already credited or for daily claims).
    crystals_credited: int = 0


@dataclass(frozen=True)
class WebhookVoteResult:
    kind: Literal["paid", "duplicate", "absorbed", "stale"]
    amount: int
    crystals_credited: int = 0


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _claimed_today_utc(last: datetime | None) -> bool:
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return last.astimezone(UTC).date() == _utc_today()


def format_pokedollars(amount: int) -> str:
    return f"{CURRENCY_SYMBOL}{amount:,}"


class WalletService:
    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random(secrets.randbits(128))

    async def get_balance(self, session: AsyncSession, discord_user_id: int) -> int:
        row = await session.get(UserPokedollars, discord_user_id)
        return 0 if row is None else row.balance

    async def _get_or_create(self, session: AsyncSession, discord_user_id: int) -> UserPokedollars:
        row = await session.get(UserPokedollars, discord_user_id)
        if row is not None:
            return row
        row = UserPokedollars(discord_user_id=discord_user_id, balance=0)
        session.add(row)
        await session.flush()
        return row

    async def set_balance(
        self,
        session: AsyncSession,
        discord_user_id: int,
        amount: int,
    ) -> int:
        """Set balance to **amount** (developer / admin tooling). Returns the new balance."""
        if amount < 0:
            msg = "amount must be non-negative"
            raise ValueError(msg)
        row = await self._get_or_create(session, discord_user_id)
        row.balance = amount
        return row.balance

    async def try_credit(
        self,
        session: AsyncSession,
        discord_user_id: int,
        amount: int,
    ) -> int:
        """Add **amount** to balance. Returns new balance."""
        if amount < 0:
            msg = "amount must be non-negative"
            raise ValueError(msg)
        if amount == 0:
            return await self.get_balance(session, discord_user_id)
        row = await self._get_or_create(session, discord_user_id)
        row.balance += amount
        return row.balance

    async def try_debit(
        self,
        session: AsyncSession,
        discord_user_id: int,
        amount: int,
    ) -> int:
        """Subtract **amount** from balance. Returns new balance, or raises ``InsufficientPokedollarsError``."""
        if amount < 0:
            msg = "amount must be non-negative"
            raise ValueError(msg)
        if amount == 0:
            return await self.get_balance(session, discord_user_id)
        row = await self._get_or_create(session, discord_user_id)
        if row.balance < amount:
            raise InsufficientPokedollarsError
        row.balance -= amount
        return row.balance

    async def try_daily_claim(
        self,
        session: AsyncSession,
        discord_user_id: int,
    ) -> DailyClaimResult:
        row = await self._get_or_create(session, discord_user_id)
        if _claimed_today_utc(row.last_daily_claim_at):
            raise AlreadyClaimedTodayError
        amount = self._rng.randint(DAILY_CLAIM_MIN, DAILY_CLAIM_MAX)
        row.balance = row.balance + amount
        row.last_daily_claim_at = datetime.now(UTC)
        return DailyClaimResult(amount=amount, new_balance=row.balance)

    async def try_vote_claim(
        self,
        session: AsyncSession,
        discord_user_id: int,
        *,
        vote_created_at: datetime,
        vote_expires_at: datetime,
    ) -> DailyClaimResult:
        """Grant vote reward once per Top.gg vote (``vote_created_at`` identifies the vote)."""
        from poke_pon_bot.services.crystals import CrystalsService

        row = await self._get_or_create(session, discord_user_id)
        if _same_topgg_vote_slice(row.last_rewarded_topgg_vote_at, vote_created_at):
            now = datetime.now(UTC)
            retry = max(0.0, (_utc(vote_expires_at) - now).total_seconds())
            raise AlreadyClaimedVoteRewardError(retry_after_seconds=retry)
        amount = self._rng.randint(VOTE_CLAIM_MIN, VOTE_CLAIM_MAX)
        row.balance = row.balance + amount
        row.last_vote_claim_at = datetime.now(UTC)
        row.last_rewarded_topgg_vote_at = _utc(vote_created_at)

        # Crystal payout shares the same per-vote dedupe (separate table tracks its own slice).
        crystals = await CrystalsService().credit_vote(
            session, discord_user_id, vote_created_at=vote_created_at
        )
        return DailyClaimResult(
            amount=amount, new_balance=row.balance, crystals_credited=crystals
        )

    async def apply_topgg_webhook_vote(
        self,
        session: AsyncSession,
        *,
        vote_id: str,
        discord_user_id: int,
        vote_created_at: datetime,
        vote_expires_at: datetime,
    ) -> WebhookVoteResult:
        """Credit voter from Top.gg ``vote.create`` webhook; idempotent on ``vote_id``."""
        vid = vote_id.strip()
        if not vid:
            return WebhookVoteResult(kind="stale", amount=0)

        existing = await session.get(TopggProcessedVote, vid)
        if existing is not None:
            return WebhookVoteResult(kind="duplicate", amount=0)

        now = datetime.now(UTC)
        ca = _utc(vote_created_at)
        exp = _utc(vote_expires_at)

        if now >= exp:
            session.add(
                TopggProcessedVote(
                    vote_id=vid,
                    discord_user_id=int(discord_user_id),
                    amount_credited=0,
                    processed_at=now,
                )
            )
            return WebhookVoteResult(kind="stale", amount=0)

        row = await self._get_or_create(session, discord_user_id)

        if _same_topgg_vote_slice(row.last_rewarded_topgg_vote_at, vote_created_at):
            session.add(
                TopggProcessedVote(
                    vote_id=vid,
                    discord_user_id=int(discord_user_id),
                    amount_credited=0,
                    processed_at=now,
                )
            )
            return WebhookVoteResult(kind="absorbed", amount=0)

        from poke_pon_bot.services.crystals import CrystalsService

        amount = self._rng.randint(VOTE_CLAIM_MIN, VOTE_CLAIM_MAX)
        row.balance = row.balance + amount
        row.last_vote_claim_at = now
        row.last_rewarded_topgg_vote_at = ca
        session.add(
            TopggProcessedVote(
                vote_id=vid,
                discord_user_id=int(discord_user_id),
                amount_credited=amount,
                processed_at=now,
            )
        )
        crystals = await CrystalsService().credit_vote(
            session, discord_user_id, vote_created_at=vote_created_at
        )
        return WebhookVoteResult(kind="paid", amount=amount, crystals_credited=crystals)

    async def get_last_drop_at(
        self, session: AsyncSession, discord_user_id: int
    ) -> datetime | None:
        row = await session.get(UserPokedollars, discord_user_id)
        if row is None:
            return None
        last = row.last_drop_at
        if last is None:
            return None
        return _utc(last)

    async def record_drop(self, session: AsyncSession, discord_user_id: int) -> None:
        row = await self._get_or_create(session, discord_user_id)
        row.last_drop_at = datetime.now(UTC)
