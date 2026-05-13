"""Top.gg vote IDs already rewarded (webhook idempotency + /vote overlap)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from poke_pon_bot.db.base import Base


class TopggProcessedVote(Base):
    """One row per Top.gg ``vote.create`` ``data.id`` we have finished processing."""

    __tablename__ = "topgg_processed_votes"

    vote_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    amount_credited: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
