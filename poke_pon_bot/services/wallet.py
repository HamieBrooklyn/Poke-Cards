"""Pokedollars balance and daily claim (UTC day)."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.pokedollars import UserPokedollars

CURRENCY_SYMBOL = "₽"
CURRENCY_NAME = "Pokedollars"
DAILY_CLAIM_MIN = 100
DAILY_CLAIM_MAX = 200


class InsufficientPokedollarsError(Exception):
    """Balance too low for the debit amount."""


class AlreadyClaimedTodayError(Exception):
    """User already used their daily claim for the current UTC calendar day."""


@dataclass(frozen=True)
class DailyClaimResult:
    amount: int
    new_balance: int


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
