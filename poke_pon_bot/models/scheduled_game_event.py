"""DB-backed scheduled game events (luck boost, double daily, spotlight promos)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base

EVENT_KIND_LUCK_BOOST = "luck_boost"
EVENT_KIND_DOUBLE_DAILY = "double_daily"
EVENT_KIND_FREE_SPOTLIGHT = "free_spotlight"
EVENT_KIND_SET_SPOTLIGHT = "set_spotlight"

EVENT_KINDS = frozenset(
    {
        EVENT_KIND_LUCK_BOOST,
        EVENT_KIND_DOUBLE_DAILY,
        EVENT_KIND_FREE_SPOTLIGHT,
        EVENT_KIND_SET_SPOTLIGHT,
    }
)


class ScheduledGameEvent(Base):
    """One scheduled or recurring in-game promotion."""

    __tablename__ = "scheduled_game_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Absolute window (UTC). Null when ``recurrence`` defines the schedule.
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ``weekly`` = repeat using window fields inside ``config_json``.
    recurrence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    created_by_discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
