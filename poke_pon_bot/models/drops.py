"""Named drop tables and per-tier weights."""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from poke_pon_bot.db.base import Base


class DropTable(Base):
    """Named pool for future banners; v1 uses `default`."""

    __tablename__ = "drop_tables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    weights = relationship(
        "DropWeight",
        back_populates="drop_table",
        cascade="all, delete-orphan",
    )


class DropWeight(Base):
    """Relative weight per rarity tier within a drop table."""

    __tablename__ = "drop_weights"
    __table_args__ = (
        UniqueConstraint(
            "drop_table_id",
            "rarity_class_id",
            name="uq_drop_weights_table_rarity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drop_table_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("drop_tables.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rarity_class_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("rarity_classes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False)

    drop_table = relationship("DropTable", back_populates="weights")
    rarity_class = relationship("RarityClass", back_populates="drop_weights")
