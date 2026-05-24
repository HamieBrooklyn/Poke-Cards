"""Website profile notification preferences."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "035_user_web_preferences"
down_revision: Union[str, None] = "034_rarity_luck_boosts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_web_preferences",
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("notify_trades", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("notify_auctions", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("notify_referrals", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_web_preferences")
