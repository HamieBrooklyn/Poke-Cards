"""Referrals 2.0 — first-pack rewards for inviter and invitee."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "054_referrals_v2"
down_revision: Union[str, None] = "053_guild_milestones"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "guild_referrals", "first_pack_rewarded_at"):
        return
    op.add_column(
        "guild_referrals",
        sa.Column("first_pack_rewarded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "guild_referrals",
        sa.Column(
            "invitee_crystals_awarded",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for col in ("invitee_crystals_awarded", "first_pack_rewarded_at"):
        if _column_exists(bind, "guild_referrals", col):
            op.drop_column("guild_referrals", col)
