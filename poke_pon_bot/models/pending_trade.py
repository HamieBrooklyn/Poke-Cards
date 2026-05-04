"""Outstanding player-to-player trade offers (Accept / Decline on Discord)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class PendingTrade(Base):
    """Initiator proposes; partner confirms in-channel. Rows are deleted when done or declined."""

    __tablename__ = "pending_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    initiator_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    partner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    give_instance_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    give_pokedollars: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    receive_instance_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    receive_pokedollars: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
