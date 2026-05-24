"""One-time interactive onboarding tutorial (DM + command checkpoints)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from poke_pon_bot.db.base import Base


class UserTutorial(Base):
    __tablename__ = "user_tutorials"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    current_step: Mapped[str] = mapped_column(String(32), nullable=False, default="welcome")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    #: Guild where Member role should be granted on completion (main server).
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
