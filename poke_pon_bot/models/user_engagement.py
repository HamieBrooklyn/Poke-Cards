"""Per-user engagement counters and Top.gg review prompt state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class UserEngagement(Base):
    __tablename__ = "user_engagement"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    successful_commands: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    topgg_review_reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    topgg_review_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
