"""Live TCG market quotes, daily history, and player investment lots."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from poke_pon_bot.db.base import Base


class CardMarketQuote(Base):
    """Latest TCGPlayer market snapshot for one catalog printing."""

    __tablename__ = "card_market_quotes"

    card_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    usd_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    usd_low_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usd_mid_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usd_high_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="tcgplayer")
    tcgplayer_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    card = relationship("Card")


class CardMarketHistory(Base):
    """One USD close per UTC day (built as we fetch live quotes)."""

    __tablename__ = "card_market_history"
    __table_args__ = (UniqueConstraint("card_id", "day", name="uq_card_market_history_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    usd_cents: Mapped[int] = mapped_column(Integer, nullable=False)


class CardInvestment(Base):
    """Open pokedollar position tracking a catalog printing's live USD price."""

    __tablename__ = "card_investments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    card_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pokedollars_in: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_usd_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    card = relationship("Card")
