"""Idempotent record of fulfilled Stripe Checkout purchases."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from poke_pon_bot.db.base import Base


class StripePurchase(Base):
    __tablename__ = "stripe_purchases"

    checkout_session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger(), nullable=False, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_granted: Mapped[int] = mapped_column(Integer(), nullable=False)
    stripe_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
