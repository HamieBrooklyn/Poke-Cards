"""Track first guild join per user to block referral leave/rejoin abuse."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "031_guild_member_seen"
down_revision: Union[str, None] = "030_guild_referrals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "guild_member_seen",
        sa.Column("guild_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("guild_member_seen")
