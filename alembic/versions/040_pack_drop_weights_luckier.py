"""Slightly raise rare+ drop weights after pack luck tuning."""

from __future__ import annotations

from alembic import op

revision: str = "040_pack_drop_weights_luckier"
down_revision: str | None = "039_pack_drop_weights_generous"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE drop_weights SET weight = CASE rarity_class_id
            WHEN 1 THEN 46000
            WHEN 2 THEN 33000
            WHEN 3 THEN 22000
            WHEN 4 THEN 15500
            WHEN 5 THEN 10000
            WHEN 6 THEN 6800
            WHEN 7 THEN 4100
            WHEN 8 THEN 2000
            WHEN 9 THEN 500
            WHEN 10 THEN 100
            ELSE weight
        END
        WHERE drop_table_id = 1
        """
    )


def downgrade() -> None:
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
