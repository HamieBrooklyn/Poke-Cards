"""Scheduled game events table."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "059_scheduled_game_events"
down_revision: Union[str, None] = "058_daily_drop_notify_default_off"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduled_game_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recurrence", sa.String(length=16), nullable=True),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("created_by_discord_user_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduled_game_events_kind", "scheduled_game_events", ["kind"])
    op.create_index(
        "ix_scheduled_game_events_enabled_starts",
        "scheduled_game_events",
        ["enabled", "starts_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_game_events_enabled_starts", table_name="scheduled_game_events")
    op.drop_index("ix_scheduled_game_events_kind", table_name="scheduled_game_events")
    op.drop_table("scheduled_game_events")
