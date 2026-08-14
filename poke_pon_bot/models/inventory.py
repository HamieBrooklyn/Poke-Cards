"""User-owned card instances."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class UserCardInstance(Base):
    """One owned copy of a catalog card (e.g. from a drop)."""

    __tablename__ = "user_card_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Stable per-copy id for display and future trades: new rows use 16 url-safe chars (96 bits);
    # existing rows may still be 32 hex. Not the same as integer primary `id`.
    public_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        unique=True,
        index=True,
    )
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
    # Times this copy has been evolved (future costs scale with this).
    evolution_stages: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grade_enchantment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    craft_uses_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auction_obtained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    card = relationship("Card", back_populates="instances")
