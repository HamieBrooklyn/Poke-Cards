"""Interactive web duel sessions (advanced PvP minigame)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base

DUEL_STATUS_INVITED = "invited"
DUEL_STATUS_ACTIVE = "active"
DUEL_STATUS_COMPLETED = "completed"
DUEL_STATUS_CANCELLED = "cancelled"
DUEL_STATUS_EXPIRED = "expired"
DUEL_STATUS_DECLINED = "declined"

DUEL_BID_CURRENCY_POKEDOLLARS = "pokedollars"
DUEL_BID_CURRENCY_CRYSTALS = "crystals"

INVITE_TTL_MINUTES = 30
ACTIVE_TTL_MINUTES = 45
# Cancel invited/active duels when nobody is connected to the WS room this long.
ABANDON_CANCEL_MINUTES = 5


class DuelSession(Base):
    __tablename__ = "duel_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    initiator_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    partner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DUEL_STATUS_INVITED,
        server_default=DUEL_STATUS_INVITED,
        index=True,
    )

    bet_currency: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DUEL_BID_CURRENCY_POKEDOLLARS,
        server_default=DUEL_BID_CURRENCY_POKEDOLLARS,
    )
    bet_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Updated at accept; used for coin-flip result and reconnect UI.
    starting_player_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Stake escrow (locked upfront on accept).
    escrow_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    escrow_amount_each: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    escrow_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escrow_paid_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Authoritative serialized match state for reconnect/resume.
    state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

