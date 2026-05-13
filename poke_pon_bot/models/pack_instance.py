"""Owned booster packs (unopened until ``opened_at`` is set)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class UserPackInstance(Base):
    """One unopened (or opened-and-flipped) pack owned by a user."""

    __tablename__ = "user_pack_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        unique=True,
        index=True,
    )
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: Set at purchase time so ``/packcat scope:server`` can scope its popularity counts.
    #: Nullable for DM purchases or rows imported before the column existed.
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    series_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("card_series.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: ``purchase_pokedollars`` / ``purchase_crystals`` / ``sku_consumable`` / ``dev``.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="purchase_pokedollars")
    obtained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    #: ``None`` = unopened. Set on first :func:`PackService.open_pack` call.
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
