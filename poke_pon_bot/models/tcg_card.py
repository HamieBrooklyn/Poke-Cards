"""Versioned complete rules data, isolated from the production catalog schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class TcgCardDefinition(Base):
    __tablename__ = "tcg_card_definitions"

    tcg_card_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("cards.tcg_card_id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    data_version: Mapped[str] = mapped_column(String(80), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
