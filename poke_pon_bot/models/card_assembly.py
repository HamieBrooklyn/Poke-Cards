"""Catalog definitions for multi-card assembly (V-UNION, BREAK halves, etc.)."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from poke_pon_bot.db.base import Base


class CardAssemblyGroup(Base):
    """One assembled card built from 2 or 4 owned piece printings."""

    __tablename__ = "card_assembly_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Stable slug (e.g. ``mewtwo_v_union_swshp``).
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    result_card_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    piece_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ``horizontal_halves`` (2) or ``quad`` (4).
    layout: Mapped[str] = mapped_column(String(32), nullable=False)
    #: How the finished card is shown: ``landscape`` (BREAK-style) or ``portrait``.
    orientation: Mapped[str] = mapped_column(String(16), nullable=False, default="portrait")

    result_card = relationship("Card", foreign_keys=[result_card_id])
    pieces = relationship(
        "CardAssemblyPiece",
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="CardAssemblyPiece.slot_index",
    )


class CardAssemblyPiece(Base):
    """One catalog printing that is a slot in an assembly group."""

    __tablename__ = "card_assembly_pieces"
    __table_args__ = (
        UniqueConstraint("group_id", "card_id", name="uq_assembly_piece_group_card"),
        UniqueConstraint("group_id", "slot_index", name="uq_assembly_piece_group_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("card_assembly_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    card_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    rotation_deg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    grid_col: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    grid_row: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    group = relationship("CardAssemblyGroup", back_populates="pieces")
    card = relationship("Card", foreign_keys=[card_id])
