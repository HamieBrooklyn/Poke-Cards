"""Vote reminders / reward DMs off by default for every user."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "064_vote_notify_default_off"
down_revision: Union[str, None] = "063_card_investing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "user_web_preferences"):
        return

    op.execute(sa.text("UPDATE user_web_preferences SET notify_vote = 0"))

    with op.batch_alter_table("user_web_preferences") as batch:
        batch.alter_column(
            "notify_vote",
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
            "notify_vote",
            server_default="1",
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
