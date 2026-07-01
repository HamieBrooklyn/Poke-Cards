"""Published catalog news (new TCG sets / card drops) for web + Discord."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base

ANNOUNCE_KIND_NEW_SET = "new_set"
ANNOUNCE_KIND_CARDS_ADDED = "cards_added"


class CatalogAnnouncement(Base):
    """One news item surfaced on the website and optionally posted to Discord."""

    __tablename__ = "catalog_announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    set_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    set_name: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_card_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sample_cards_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    card_ids_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    catalog_set_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pokedex_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    packs_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discord_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
