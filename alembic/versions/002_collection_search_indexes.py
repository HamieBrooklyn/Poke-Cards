"""Indexes to speed up collection search (user + time, name, rarity)."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "002_collection_indexes"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_user_card_instances_user_obtained",
        "user_card_instances",
        ["discord_user_id", "obtained_at"],
        unique=False,
    )
    op.create_index("ix_cards_name", "cards", ["name"], unique=False)
    op.create_index("ix_cards_tcg_rarity", "cards", ["tcg_rarity"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cards_tcg_rarity", table_name="cards")
    op.drop_index("ix_cards_name", table_name="cards")
    op.drop_index("ix_user_card_instances_user_obtained", table_name="user_card_instances")
