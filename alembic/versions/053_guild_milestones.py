"""Guild / server milestones — stats, settings, member counters."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "053_guild_milestones"
down_revision: Union[str, None] = "052_crystal_sinks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "guild_milestone_settings"):
        op.create_table(
            "guild_milestone_settings",
            sa.Column("guild_id", sa.BigInteger(), primary_key=True),
            sa.Column("announcement_channel_id", sa.BigInteger(), nullable=True),
            sa.Column("top_trader_role_id", sa.BigInteger(), nullable=True),
            sa.Column("top_collector_role_id", sa.BigInteger(), nullable=True),
            sa.Column("auto_roles_enabled", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
        )
    if not _table_exists(bind, "guild_stats"):
        op.create_table(
            "guild_stats",
            sa.Column("guild_id", sa.BigInteger(), primary_key=True),
            sa.Column("packs_opened", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("trades_completed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_milestone_tier_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
        )
    if not _table_exists(bind, "guild_member_stats"):
        op.create_table(
            "guild_member_stats",
            sa.Column("guild_id", sa.BigInteger(), primary_key=True),
            sa.Column("discord_user_id", sa.BigInteger(), primary_key=True),
            sa.Column("packs_opened", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("trades_completed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_guild_member_stats_guild_packs",
            "guild_member_stats",
            ["guild_id", "packs_opened"],
        )
        op.create_index(
            "ix_guild_member_stats_guild_trades",
            "guild_member_stats",
            ["guild_id", "trades_completed"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "guild_member_stats"):
        op.drop_index("ix_guild_member_stats_guild_trades", table_name="guild_member_stats")
        op.drop_index("ix_guild_member_stats_guild_packs", table_name="guild_member_stats")
        op.drop_table("guild_member_stats")
    if _table_exists(bind, "guild_stats"):
        op.drop_table("guild_stats")
    if _table_exists(bind, "guild_milestone_settings"):
        op.drop_table("guild_milestone_settings")
