"""Per-user wishlist of catalog cards (by ``cards.id``). Used for /cv ★ and drop pings."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class UserCardWishlist(Base):
    """User wants this catalog printing; other server members get pinged on /cd packs containing it."""

    __tablename__ = "user_card_wishlist"
    __table_args__ = (UniqueConstraint("discord_user_id", "card_id", name="uq_user_card_wishlist_user_card"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    card_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    card = relationship("Card")

