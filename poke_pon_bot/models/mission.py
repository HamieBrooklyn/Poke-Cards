"""Per-user daily and weekly mission assignments."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class UserMission(Base):
    """One rolled mission slot for a user in a daily or weekly period."""

    __tablename__ = "user_missions"
    __table_args__ = (
        UniqueConstraint(
            "discord_user_id",
            "period_type",
            "period_key",
            "slot",
            name="uq_user_missions_period_slot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: ``daily`` or ``weekly``
    period_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: UTC day ``YYYY-MM-DD`` or ISO week ``YYYY-Www``
    period_key: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    #: ``0``–``2`` for daily; ``0`` for weekly
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ``drop_uses`` | ``claim_set`` | ``duel_wins`` | ``obtain_card``
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    set_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    set_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    card_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("cards.id", ondelete="SET NULL"),
        nullable=True,
    )
    card_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reward_crystals: Mapped[int] = mapped_column(Integer, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
