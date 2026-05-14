"""Interactive web trade sessions — both sides add/remove cards and currency live."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base

TRADE_STATUS_INVITED = "invited"
TRADE_STATUS_ACTIVE = "active"
TRADE_STATUS_COMPLETED = "completed"
TRADE_STATUS_CANCELLED = "cancelled"
TRADE_STATUS_EXPIRED = "expired"
TRADE_STATUS_DECLINED = "declined"

INVITE_TTL_MINUTES = 30
ACTIVE_TTL_MINUTES = 30


class TradeSession(Base):
    """Two-sided interactive trade managed through the website."""

    __tablename__ = "trade_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    initiator_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    partner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TRADE_STATUS_INVITED, server_default=TRADE_STATUS_INVITED
    )

    initiator_card_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    initiator_pokedollars: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    initiator_crystals: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    initiator_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    partner_card_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    partner_pokedollars: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    partner_crystals: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    partner_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
