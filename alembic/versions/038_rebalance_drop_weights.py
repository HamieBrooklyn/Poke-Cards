"""Rebalance default drop weights — better odds for rare+ tiers in packs."""

from __future__ import annotations

from alembic import op

revision: str = "038_rebalance_drop_weights"
down_revision: str | None = "037_crafting_subtypes_uses"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE drop_weights SET weight = CASE rarity_class_id
            WHEN 1 THEN 70000
            WHEN 2 THEN 45000
            WHEN 3 THEN 28000
            WHEN 4 THEN 15000
            WHEN 5 THEN 9000
            WHEN 6 THEN 5500
            WHEN 7 THEN 3000
            WHEN 8 THEN 1200
            WHEN 9 THEN 250
            WHEN 10 THEN 50
            ELSE weight
        END
        WHERE drop_table_id = 1
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE drop_weights SET weight = CASE rarity_class_id
            WHEN 1 THEN 100000
            WHEN 2 THEN 60000
            WHEN 3 THEN 35000
            WHEN 4 THEN 8000
            WHEN 5 THEN 4000
            WHEN 6 THEN 2500
            WHEN 7 THEN 1200
            WHEN 8 THEN 400
            WHEN 9 THEN 40
            WHEN 10 THEN 2
            ELSE weight
        END
        WHERE drop_table_id = 1
        """
    )
