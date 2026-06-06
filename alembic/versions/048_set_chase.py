"""Seasonal set chase — community progress + personal rewards."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "048_set_chase"
down_revision: Union[str, None] = "047_duel_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "set_chase_seasons"):
        op.create_table(
            "set_chase_seasons",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("season_key", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=256), nullable=False),
            sa.Column("set_code", sa.String(length=32), nullable=False),
            sa.Column("set_name", sa.String(length=256), nullable=False, server_default=""),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("global_target", sa.Integer(), nullable=False, server_default="5000"),
            sa.Column("global_claims", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("drop_boost_percent", sa.Integer(), nullable=False, server_default="20"),
            sa.Column(
                "completion_threshold_pct", sa.Integer(), nullable=False, server_default="80"
            ),
            sa.Column("reward_crystals", sa.Integer(), nullable=False, server_default="50"),
            sa.Column("reward_pokedollars", sa.Integer(), nullable=False, server_default="2500"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("season_key", name="uq_set_chase_seasons_key"),
        )
        op.create_index("ix_set_chase_seasons_set_code", "set_chase_seasons", ["set_code"])

    if not _table_exists(bind, "set_chase_reward_claims"):
        op.create_table(
            "set_chase_reward_claims",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("season_id", sa.Integer(), nullable=False),
            sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "claimed_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "season_id",
                "discord_user_id",
                name="uq_set_chase_reward_claims_user",
            ),
        )
        op.create_index(
            "ix_set_chase_reward_claims_season_id",
            "set_chase_reward_claims",
            ["season_id"],
        )
        op.create_index(
            "ix_set_chase_reward_claims_discord_user_id",
            "set_chase_reward_claims",
            ["discord_user_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "set_chase_reward_claims"):
        op.drop_index("ix_set_chase_reward_claims_discord_user_id", "set_chase_reward_claims")
        op.drop_index("ix_set_chase_reward_claims_season_id", "set_chase_reward_claims")
        op.drop_table("set_chase_reward_claims")
    if _table_exists(bind, "set_chase_seasons"):
        op.drop_index("ix_set_chase_seasons_set_code", "set_chase_seasons")
        op.drop_table("set_chase_seasons")
