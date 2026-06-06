"""Add notify_missions preference for mission completion DMs."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "050_notify_missions"
down_revision: Union[str, None] = "049_set_chase_community"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def _column_exists(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "user_web_preferences"):
        if not _column_exists(bind, "user_web_preferences", "notify_missions"):
            op.add_column(
                "user_web_preferences",
                sa.Column(
                    "notify_missions",
                    sa.Boolean(),
                    nullable=False,
                    server_default="1",
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "user_web_preferences"):
        if _column_exists(bind, "user_web_preferences", "notify_missions"):
            op.drop_column("user_web_preferences", "notify_missions")
