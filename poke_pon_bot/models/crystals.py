"""User Crystal balance — secondary currency, primarily earned via Top.gg votes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from poke_pon_bot.db.base import Base


class UserCrystals(Base):
    """One row per Discord user: Crystal balance + vote-slice timestamps for dedupe.

    Mirrors :class:`UserPokedollars`: the dedupe column is shared in spirit with the Pokedollar
    table so a single Top.gg vote credits **both** currencies once and only once.
    """

    __tablename__ = "user_crystals"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_vote_claim_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_rewarded_topgg_vote_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
