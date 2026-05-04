"""Catalog evolution links and per-copy evolution count.

SQLite cannot add a named self-referential FK to an existing table without a table
rebuild; integrity is enforced by the ORM. The column + index are created here.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "004_card_evolution"
down_revision: Union[str, None] = "003_user_pokedollars"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    card_cols = _table_columns(bind, "cards")
    if "evolves_to_names" not in card_cols:
        op.add_column("cards", sa.Column("evolves_to_names", sa.JSON(), nullable=True))
    if "evolves_to_card_id" not in card_cols:
        op.add_column(
            "cards",
            sa.Column("evolves_to_card_id", sa.Integer(), nullable=True),
        )
    if "evolution_stages" not in _table_columns(bind, "user_card_instances"):
        op.add_column(
            "user_card_instances",
            sa.Column("evolution_stages", sa.Integer(), nullable=False, server_default="0"),
        )
    inames = {ix["name"] for ix in inspect(bind).get_indexes("cards") if ix.get("name")}
    if "ix_cards_evolves_to_card_id" not in inames:
        op.create_index("ix_cards_evolves_to_card_id", "cards", ["evolves_to_card_id"], unique=False)


def downgrade() -> None:
    op.drop_column("user_card_instances", "evolution_stages")
    op.drop_index("ix_cards_evolves_to_card_id", table_name="cards")
    op.drop_column("cards", "evolves_to_card_id")
    op.drop_column("cards", "evolves_to_names")
