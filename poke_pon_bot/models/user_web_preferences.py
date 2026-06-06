"""Per-user notification preferences for the PokePon website."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class UserWebPreferences(Base):
    __tablename__ = "user_web_preferences"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    notify_trades: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    notify_auctions: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    notify_referrals: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    notify_missions: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    notify_wishlist_market: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    notify_browser: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    notify_daily: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    notify_vote: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    notify_drop: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    daily_reminder_sent_key: Mapped[str | None] = mapped_column(String(10), nullable=True)
    vote_reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    drop_reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    wishlist_alert_max_pokedollars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wishlist_alert_max_crystals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leaderboard_frame: Mapped[str | None] = mapped_column(String(32), nullable=True)
    unlocked_leaderboard_frames: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
