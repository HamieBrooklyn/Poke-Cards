"""Saved combat deck (ordered owned card instances) for duels."""

from __future__ import annotations

from sqlalchemy import BigInteger, JSON
from sqlalchemy.orm import Mapped, mapped_column

from poke_pon_bot.db.base import Base


class UserCombatDeck(Base):
    """Up to six ``UserCardInstance.id`` values in bench order (index 0 = leads)."""

    __tablename__ = "user_combat_decks"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_ids: Mapped[list] = mapped_column(JSON, nullable=False)
