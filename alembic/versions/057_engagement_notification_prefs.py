"""Engagement notification prefs (daily, vote, card drop) + reminder dedupe fields."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "057_engagement_notification_prefs"
down_revision: Union[str, None] = "056_user_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def _column_exists(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "user_web_preferences"):
        return

    bool_cols = (
        ("notify_daily", "1"),
        ("notify_vote", "1"),
        ("notify_drop", "1"),
    )
    for name, default in bool_cols:
        if not _column_exists(bind, "user_web_preferences", name):
            op.add_column(
                "user_web_preferences",
                sa.Column(
                    name,
                    sa.Boolean(),
                    nullable=False,
                    server_default=default,
                ),
            )

    if not _column_exists(bind, "user_web_preferences", "daily_reminder_sent_key"):
        op.add_column(
            "user_web_preferences",
            sa.Column("daily_reminder_sent_key", sa.String(length=10), nullable=True),
        )
    if not _column_exists(bind, "user_web_preferences", "vote_reminder_sent_at"):
        op.add_column(
            "user_web_preferences",
            sa.Column("vote_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _column_exists(bind, "user_web_preferences", "drop_reminder_sent_at"):
        op.add_column(
            "user_web_preferences",
            sa.Column("drop_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "user_web_preferences"):
        return
    for col in (
        "drop_reminder_sent_at",
        "vote_reminder_sent_at",
        "daily_reminder_sent_key",
        "notify_drop",
        "notify_vote",
        "notify_daily",
    ):
        if _column_exists(bind, "user_web_preferences", col):
            op.drop_column("user_web_preferences", col)
