"""Guild invite referrals and cd-use progress toward inviter crystal rewards."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "030_guild_referrals"
down_revision: Union[str, None] = "029_user_engagement"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "guild_referrals",
        sa.Column("invitee_discord_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("inviter_discord_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("cd_uses", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("crystals_awarded", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index(
        "ix_guild_referrals_inviter_discord_id",
        "guild_referrals",
        ["inviter_discord_id"],
    )
    op.create_index(
        "ix_guild_referrals_guild_id",
        "guild_referrals",
        ["guild_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_guild_referrals_guild_id", table_name="guild_referrals")
    op.drop_index("ix_guild_referrals_inviter_discord_id", table_name="guild_referrals")
    op.drop_table("guild_referrals")
