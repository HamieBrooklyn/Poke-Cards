"""User notification inbox + browser alert opt-in."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "056_user_notifications"
down_revision: Union[str, None] = "055_tutorial_quest_chain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def _column_exists(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "user_notifications"):
        op.create_table(
            "user_notifications",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("title", sa.String(length=256), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("href", sa.String(length=512), nullable=True),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_user_notifications_discord_user_id",
            "user_notifications",
            ["discord_user_id"],
        )
        op.create_index(
            "ix_user_notifications_created_at",
            "user_notifications",
            ["created_at"],
        )
        op.create_index(
            "ix_user_notifications_user_unread",
            "user_notifications",
            ["discord_user_id", "read_at"],
        )

    if _table_exists(bind, "user_web_preferences"):
        if not _column_exists(bind, "user_web_preferences", "notify_browser"):
            op.add_column(
                "user_web_preferences",
                sa.Column(
                    "notify_browser",
                    sa.Boolean(),
                    nullable=False,
                    server_default="0",
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "user_web_preferences"):
        if _column_exists(bind, "user_web_preferences", "notify_browser"):
            op.drop_column("user_web_preferences", "notify_browser")
    if _table_exists(bind, "user_notifications"):
        op.drop_index("ix_user_notifications_user_unread", table_name="user_notifications")
        op.drop_index("ix_user_notifications_created_at", table_name="user_notifications")
        op.drop_index("ix_user_notifications_discord_user_id", table_name="user_notifications")
        op.drop_table("user_notifications")
