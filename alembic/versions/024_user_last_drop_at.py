"""Persist pack drop cooldown timestamp per user."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "024_user_last_drop_at"
down_revision: Union[str, None] = "023_instance_favorite"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind, "user_pokedollars")
    if "last_drop_at" not in cols:
        op.add_column(
            "user_pokedollars",
            sa.Column("last_drop_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "last_drop_at" in _table_columns(bind, "user_pokedollars"):
        op.drop_column("user_pokedollars", "last_drop_at")
