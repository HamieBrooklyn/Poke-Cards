"""Per-user Discord invite code used to attribute referrals.

Each row maps one Discord user to a personal invite code in a specific guild.
When a new member joins via that code we know exactly who invited them — far
more reliable than reading ``Invite.inviter`` (which is empty for vanity URLs
and ambiguous when many users share the same link).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from poke_pon_bot.db.base import Base


class UserInviteCode(Base):
    __tablename__ = "user_invite_codes"
    __table_args__ = (
        UniqueConstraint("guild_id", "invite_code", name="uq_user_invite_codes_guild_code"),
    )

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    invite_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
