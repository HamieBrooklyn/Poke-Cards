"""Persist per-user tutorial progress (one-time onboarding)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "032_user_tutorial"
down_revision: Union[str, None] = "031_guild_member_seen"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_tutorials",
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("current_step", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("user_tutorials")
