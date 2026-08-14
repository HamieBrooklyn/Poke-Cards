"""Per-copy grade enchantment film rolled with PSA-style grading."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "062_grade_enchantment"
down_revision: Union[str, None] = "061_catalog_news_card_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind, "user_card_instances")
    if "grade_enchantment" not in cols:
        op.add_column(
            "user_card_instances",
            sa.Column("grade_enchantment", sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind, "user_card_instances")
    if "grade_enchantment" in cols:
        op.drop_column("user_card_instances", "grade_enchantment")
