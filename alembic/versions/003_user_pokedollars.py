"""Pokedollars wallet and daily claim timestamp."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_user_pokedollars"
down_revision: Union[str, None] = "002_collection_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_pokedollars",
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_daily_claim_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("discord_user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_pokedollars")
