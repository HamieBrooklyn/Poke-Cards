"""Rarity tiers and optional TCG-string overrides."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from poke_pon_bot.db.base import Base


class RarityClass(Base):
    """Normalized drop tier (maps from many printed TCG rarities)."""

    __tablename__ = "rarity_classes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    cards = relationship("Card", back_populates="rarity_class")
    drop_weights = relationship("DropWeight", back_populates="rarity_class")


class TcgRarityMapping(Base):
    """Optional DB override: exact printed TCG rarity string -> tier."""

    __tablename__ = "tcg_rarity_mappings"
    __table_args__ = (UniqueConstraint("tcg_rarity", name="uq_tcg_rarity_mappings_tcg_rarity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tcg_rarity: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    rarity_class_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("rarity_classes.id", ondelete="CASCADE"),
        nullable=False,
    )

    rarity_class = relationship("RarityClass")
