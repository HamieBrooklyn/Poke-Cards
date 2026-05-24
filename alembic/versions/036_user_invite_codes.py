"""Personal Discord invite codes for referral tracking."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "036_user_invite_codes"
down_revision: Union[str, None] = "035_user_web_preferences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_invite_codes",
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("invite_code", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "guild_id", "invite_code", name="uq_user_invite_codes_guild_code"
        ),
    )
    op.create_index(
        "ix_user_invite_codes_guild_id",
        "user_invite_codes",
        ["guild_id"],
    )
    op.create_index(
        "ix_user_invite_codes_invite_code",
        "user_invite_codes",
        ["invite_code"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_invite_codes_invite_code", table_name="user_invite_codes")
    op.drop_index("ix_user_invite_codes_guild_id", table_name="user_invite_codes")
    op.drop_table("user_invite_codes")
