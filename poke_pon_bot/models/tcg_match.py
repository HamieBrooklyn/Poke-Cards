"""Versioned TCG matches, private command history, and saved 60-card decks.

These tables deliberately do not reuse legacy combat decks or wagered duels.
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Integer, JSON, String, UniqueConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class TcgLobby(Base):
    __tablename__ = "tcg_lobbies"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True, nullable=False)
    host_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    guest_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(20), default="waiting", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    host_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    guest_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TcgSavedDeck(Base):
    __tablename__ = "tcg_saved_decks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    entries: Mapped[list] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TcgCommand(Base):
    """Private audit/replay records; never directly serialized to a client."""
    __tablename__ = "tcg_commands"
    __table_args__ = (UniqueConstraint("lobby_id", "actor_id", "command_id", name="uq_tcg_command"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lobby_id: Mapped[str] = mapped_column(String(32), ForeignKey("tcg_lobbies.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    command_id: Mapped[str] = mapped_column(String(80), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Each committed transition captures random outcomes and hidden choices,
    # allowing exact recovery/replay without exposing a random seed.
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
