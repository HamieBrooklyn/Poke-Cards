"""Further rebalance pack drop weights — more rare+ pulls per booster."""

from __future__ import annotations

from alembic import op

revision: str = "039_pack_drop_weights_generous"
down_revision: str | None = "038_rebalance_drop_weights"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE drop_weights SET weight = CASE rarity_class_id
            WHEN 1 THEN 50000
            WHEN 2 THEN 36000
            WHEN 3 THEN 24000
            WHEN 4 THEN 17000
            WHEN 5 THEN 11000
            WHEN 6 THEN 7500
            WHEN 7 THEN 4500
            WHEN 8 THEN 2200
            WHEN 9 THEN 550
            WHEN 10 THEN 110
            ELSE weight
        END
        WHERE drop_table_id = 1
        """
    )


def downgrade() -> None:
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
