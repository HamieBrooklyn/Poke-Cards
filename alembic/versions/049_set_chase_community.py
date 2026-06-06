"""Set chase community goal payout — participants + season payout timestamp."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "049_set_chase_community"
down_revision: Union[str, None] = "048_set_chase"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def _column_exists(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "set_chase_seasons"):
        if not _column_exists(bind, "set_chase_seasons", "community_participation_crystals"):
            op.add_column(
                "set_chase_seasons",
                sa.Column(
                    "community_participation_crystals",
                    sa.Integer(),
                    nullable=False,
                    server_default="15",
                ),
            )
        if not _column_exists(bind, "set_chase_seasons", "community_goal_reached_at"):
            op.add_column(
                "set_chase_seasons",
                sa.Column("community_goal_reached_at", sa.DateTime(timezone=True), nullable=True),
            )

    if not _table_exists(bind, "set_chase_participants"):
        op.create_table(
            "set_chase_participants",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("season_id", sa.Integer(), nullable=False),
            sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
            sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "first_claim_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column("community_reward_paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "season_id",
                "discord_user_id",
                name="uq_set_chase_participants_season_user",
            ),
        )
        op.create_index(
            "ix_set_chase_participants_season_id",
            "set_chase_participants",
            ["season_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "set_chase_participants"):
        op.drop_index("ix_set_chase_participants_season_id", "set_chase_participants")
        op.drop_table("set_chase_participants")
    if _table_exists(bind, "set_chase_seasons"):
        if _column_exists(bind, "set_chase_seasons", "community_goal_reached_at"):
            op.drop_column("set_chase_seasons", "community_goal_reached_at")
        if _column_exists(bind, "set_chase_seasons", "community_participation_crystals"):
            op.drop_column("set_chase_seasons", "community_participation_crystals")
