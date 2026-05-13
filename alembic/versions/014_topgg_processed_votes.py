"""Idempotent Top.gg vote webhook payouts (vote_id primary key)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_topgg_processed_votes"
down_revision: Union[str, None] = "013_topgg_vote_dedupe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "topgg_processed_votes",
        sa.Column("vote_id", sa.String(length=48), primary_key=True, nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_credited", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_topgg_processed_votes_discord_user_id",
        "topgg_processed_votes",
        ["discord_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_topgg_processed_votes_discord_user_id", table_name="topgg_processed_votes")
    op.drop_table("topgg_processed_votes")
