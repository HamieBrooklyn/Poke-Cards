"""Per-guild pack milestones, member stats, and optional top-role automation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class GuildMilestoneSettings(Base):
    """Admin-configured announcement channel and optional top-member roles."""

    __tablename__ = "guild_milestone_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    announcement_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    top_trader_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    top_collector_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    auto_roles_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class GuildStats(Base):
    """Aggregate counters for a Discord server."""

    __tablename__ = "guild_stats"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    packs_opened: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    trades_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Index into ``PACK_MILESTONE_TIERS`` last announced (0 = none).
    last_milestone_tier_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class GuildMemberStats(Base):
    """Per-member contribution inside one server."""

    __tablename__ = "guild_member_stats"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    packs_opened: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    trades_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
