"""User Pokedollars balance (spending hooks can be added later)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from poke_pon_bot.db.base import Base


class UserPokedollars(Base):
    """One row per Discord user: balance and daily-claim clock."""

    __tablename__ = "user_pokedollars"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_daily_claim_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
