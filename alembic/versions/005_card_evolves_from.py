"""Store API `evolvesFrom` for backfilling `evolves_to_card_id` on Basics."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "005_evolves_from"
down_revision: Union[str, None] = "004_card_evolution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "evolves_from" not in _table_columns(bind, "cards"):
        op.add_column(
            "cards",
            sa.Column("evolves_from", sa.String(512), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("cards", "evolves_from")
