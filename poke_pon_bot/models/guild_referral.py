"""Server invite referrals — track join via invite and reward inviter after invitee uses ``cd``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class GuildReferral(Base):
    """One row per referred member (first tracked join)."""

    __tablename__ = "guild_referrals"

    invitee_discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    inviter_discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    cd_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: Set when the invitee hits the ``cd`` threshold (reward may be 0 if inviter is at cap).
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    crystals_awarded: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
