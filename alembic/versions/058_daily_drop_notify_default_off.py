"""Default daily and card-drop reminders off (opt-in)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "058_daily_drop_notify_default_off"
down_revision: Union[str, None] = "057_engagement_notification_prefs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "user_web_preferences"):
        return

    op.execute(
        sa.text(
            "UPDATE user_web_preferences SET notify_daily = 0, notify_drop = 0"
        )
    )

    with op.batch_alter_table("user_web_preferences") as batch:
        batch.alter_column(
            "notify_daily",
            server_default="0",
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
        batch.alter_column(
            "notify_drop",
            server_default="0",
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "user_web_preferences"):
        return

    with op.batch_alter_table("user_web_preferences") as batch:
        batch.alter_column(
            "notify_daily",
            server_default="1",
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
        batch.alter_column(
            "notify_drop",
            server_default="1",
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
