"""User-owned card instances."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class UserCardInstance(Base):
    """One owned copy of a catalog card (e.g. from a drop)."""

    __tablename__ = "user_card_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    card_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    obtained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="drop")

    card = relationship("Card", back_populates="instances")
