"""Global or per-guild rarity luck for /cd, booster packs, and wild duels."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from poke_pon_bot.db.base import Base


class RarityLuckBoost(Base):
    """One row per scope: ``guild_id`` NULL = global, else that Discord guild."""

    __tablename__ = "rarity_luck_boosts"
    __table_args__ = (UniqueConstraint("guild_id", name="uq_rarity_luck_boosts_guild_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    luck_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by_discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
