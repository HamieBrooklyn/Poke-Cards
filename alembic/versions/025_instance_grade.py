"""Per-copy PSA-style grade (1–10) on owned instances."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "025_instance_grade"
down_revision: Union[str, None] = "024_user_last_drop_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind, "user_card_instances")
    if "grade" not in cols:
        op.add_column("user_card_instances", sa.Column("grade", sa.Integer(), nullable=True))
    if "graded_at" not in cols:
        op.add_column(
            "user_card_instances",
            sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind, "user_card_instances")
    if "graded_at" in cols:
        op.drop_column("user_card_instances", "graded_at")
    if "grade" in cols:
        op.drop_column("user_card_instances", "grade")
