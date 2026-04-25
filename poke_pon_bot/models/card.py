"""TCG card catalog rows."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from poke_pon_bot.db.base import Base


class Card(Base):
    """One Pokémon TCG printing (real card) from the public API."""

    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tcg_card_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    set_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    set_name: Mapped[str] = mapped_column(String(256), nullable=False)
    collector_number: Mapped[str] = mapped_column(String(32), nullable=False)
    tcg_rarity: Mapped[str | None] = mapped_column(String(256), nullable=True)
    image_small_url: Mapped[str] = mapped_column(Text, nullable=False)
    image_large_url: Mapped[str] = mapped_column(Text, nullable=False)
    supertype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hp: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dex_numbers: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)

    rarity_class_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("rarity_classes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    rarity_class = relationship("RarityClass", back_populates="cards")
    instances = relationship("UserCardInstance", back_populates="card")
