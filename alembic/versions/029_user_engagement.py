"""Track command usage and Top.gg review reminder state per user."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "029_user_engagement"
down_revision: Union[str, None] = "028_stripe_purchases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_engagement",
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("successful_commands", sa.Integer(), server_default="0", nullable=False),
        sa.Column("topgg_review_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("topgg_review_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_engagement")
