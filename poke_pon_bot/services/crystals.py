"""Crystals balance — secondary currency, primarily earned through Top.gg votes.

Mirrors :class:`WalletService` for Pokedollars but on the ``user_crystals`` table. Vote
crediting is delegated through ``WalletService.apply_topgg_webhook_vote`` /
``WalletService.try_vote_claim`` so a single vote credits both currencies once.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.crystals import UserCrystals

CRYSTAL_CURRENCY_NAME = "Crystals"
CRYSTAL_SYMBOL = "\U0001F48E"
CRYSTAL_VOTE_REWARD = 5


def format_crystals(amount: int) -> str:
    return f"{CRYSTAL_SYMBOL} {amount:,}"


class InsufficientCrystalsError(Exception):
    """Crystal balance too low for the debit amount."""


class CrystalsService:
    """Read/write helpers for the ``user_crystals`` table."""

    async def get_balance(self, session: AsyncSession, discord_user_id: int) -> int:
        row = await session.get(UserCrystals, discord_user_id)
        return 0 if row is None else row.balance

    async def _get_or_create(
        self, session: AsyncSession, discord_user_id: int
    ) -> UserCrystals:
        row = await session.get(UserCrystals, discord_user_id)
        if row is not None:
            return row
        row = UserCrystals(discord_user_id=discord_user_id, balance=0)
        session.add(row)
        await session.flush()
        return row

    async def set_balance(
        self,
        session: AsyncSession,
        discord_user_id: int,
        amount: int,
    ) -> int:
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
        if amount < 0:
            msg = "amount must be non-negative"
            raise ValueError(msg)
        if amount == 0:
            return await self.get_balance(session, discord_user_id)
        row = await self._get_or_create(session, discord_user_id)
        if row.balance < amount:
            raise InsufficientCrystalsError
        row.balance -= amount
        return row.balance

    async def credit_vote(
        self,
        session: AsyncSession,
        discord_user_id: int,
        *,
        vote_created_at: datetime,
        amount: int = CRYSTAL_VOTE_REWARD,
    ) -> int:
        """Credit ``amount`` Crystals for a vote, deduping on ``vote_created_at``.

        Returns the **crystals actually credited** (0 if this vote slice already paid).
        Mirrors how Pokedollar vote crediting tracks a per-user "last paid vote" timestamp.
        """
        from poke_pon_bot.services.wallet import _same_topgg_vote_slice, _utc

        row = await self._get_or_create(session, discord_user_id)
        if _same_topgg_vote_slice(row.last_rewarded_topgg_vote_at, vote_created_at):
            return 0
        row.balance = row.balance + amount
        row.last_vote_claim_at = datetime.now(UTC)
        row.last_rewarded_topgg_vote_at = _utc(vote_created_at)
        return amount
