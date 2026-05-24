"""Restore /cd drop weights — pack rebalance migrations only targeted boosters via luck."""

from __future__ import annotations

from alembic import op

revision: str = "041_restore_cd_drop_weights"
down_revision: str | None = "040_pack_drop_weights_luckier"
branch_labels: str | None = None
depends_on: str | None = None

# Pre-038 defaults (classic /cd balance). Booster packs use PACK_OPEN_LUCK_PERCENT instead.
_CD_DROP_WEIGHTS: tuple[tuple[int, int], ...] = (
    (1, 100000),
    (2, 60000),
    (3, 35000),
    (4, 8000),
    (5, 4000),
    (6, 2500),
    (7, 1200),
    (8, 400),
    (9, 40),
    (10, 2),
)


def _weight_case(rows: tuple[tuple[int, int], ...]) -> str:
    lines = ["CASE rarity_class_id"]
    for rid, w in rows:
        lines.append(f"            WHEN {rid} THEN {w}")
    lines.append("            ELSE weight")
    return "\n".join(lines)


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE drop_weights SET weight = {_weight_case(_CD_DROP_WEIGHTS)}
        END
        WHERE drop_table_id = 1
        """
    )


def downgrade() -> None:
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
