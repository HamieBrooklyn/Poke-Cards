"""Per-copy favorite flag on owned instances."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "023_instance_favorite"
down_revision: Union[str, None] = "022_trade_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind, "user_card_instances")
    if "is_favorite" not in cols:
        op.add_column(
            "user_card_instances",
            sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "is_favorite" in _table_columns(bind, "user_card_instances"):
        op.drop_column("user_card_instances", "is_favorite")
