"""Global or per-guild ``/cd`` drop theme (pack series, set, or Pokémon name)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from poke_pon_bot.db.base import Base

THEME_KIND_SERIES = "series"
THEME_KIND_SET = "set"
THEME_KIND_POKEMON = "pokemon"
THEME_KINDS = frozenset({THEME_KIND_SERIES, THEME_KIND_SET, THEME_KIND_POKEMON})


class CdDropTheme(Base):
    """One active theme per scope: ``guild_id`` NULL = global, else that Discord guild."""

    __tablename__ = "cd_drop_themes"
    __table_args__ = (UniqueConstraint("guild_id", name="uq_cd_drop_themes_guild_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    theme_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    theme_value: Mapped[str] = mapped_column(String(128), nullable=False)
    chance_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by_discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
