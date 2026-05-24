"""Timed auctions: minimum bid, optional high bidder, settlement when ``ends_at`` passes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base

AUCTION_STATUS_ACTIVE = "active"
AUCTION_STATUS_ENDED_SOLD = "ended_sold"
AUCTION_STATUS_ENDED_NO_BIDS = "ended_no_bids"

AUCTION_BID_CURRENCY_POKEDOLLARS = "pokedollars"
AUCTION_BID_CURRENCY_CRYSTALS = "crystals"


class CardAuction(Base):
    """Active listing: instance stays with seller until a winning bid is settled."""

    __tablename__ = "card_auctions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seller_discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # Guild the listing was created in (None = DM / unknown). Used by `/auction search scope:server`.
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user_card_instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    price_pokedollars: Mapped[int] = mapped_column(Integer, nullable=False)
    bid_currency: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=AUCTION_BID_CURRENCY_POKEDOLLARS,
        server_default=AUCTION_BID_CURRENCY_POKEDOLLARS,
    )
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    high_bidder_discord_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    high_bid_pokedollars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class AuctionBid(Base):
    """Immutable bid history row for web auction detail."""

    __tablename__ = "auction_bids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    auction_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("card_auctions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bidder_discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
