"""In-flight /cd channel drops — survives bot restarts until expiry or fully claimed."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class PendingChannelDrop(Base):
    """Snapshot of a public ``PackPickView`` tied to a Discord message."""

    __tablename__ = "pending_channel_drops"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    issuer_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    opener_mention: Mapped[str] = mapped_column(String(256), nullable=False)

    card_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    deadline_unix: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    claim_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_grabs_per_user: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    private_pack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    mission_block: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    content_prefix: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    slot_claimer: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    slot_public_ids: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    finished: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    reroll_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
