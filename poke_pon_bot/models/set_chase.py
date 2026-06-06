"""Seasonal featured-set chase — community progress + personal completion rewards."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class SetChaseSeason(Base):
    __tablename__ = "set_chase_seasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    set_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    set_name: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    global_target: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5000")
    global_claims: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    drop_boost_percent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    completion_threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False, server_default="80")

    reward_crystals: Mapped[int] = mapped_column(Integer, nullable=False, server_default="50")
    reward_pokedollars: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2500")

    community_participation_crystals: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="15"
    )
    community_goal_reached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    enabled: Mapped[bool] = mapped_column(nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SetChaseRewardClaim(Base):
    __tablename__ = "set_chase_reward_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SetChaseParticipant(Base):
    """Players who claimed at least one featured-set card from `/cd` this season."""

    __tablename__ = "set_chase_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    first_claim_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    community_reward_paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
