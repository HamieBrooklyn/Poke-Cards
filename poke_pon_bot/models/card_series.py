"""Series umbrella that groups multiple TCG sets for booster-style packs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class CardSeries(Base):
    """Pack pool definition: name, art, price, and how many slots a pack of this series rolls."""

    __tablename__ = "card_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    crystal_price: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    pack_art_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    cards_per_pack: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    code_cards_per_pack: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class CardSeriesSet(Base):
    """Junction: which TCG ``set_code`` values belong to a given series."""

    __tablename__ = "card_series_sets"
    __table_args__ = (
        UniqueConstraint("series_id", "set_code", name="uq_card_series_sets_series_setcode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("card_series.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    set_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
