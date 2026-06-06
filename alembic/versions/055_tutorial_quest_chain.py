"""Tutorial quest chain — track crystals earned during onboarding."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "055_tutorial_quest_chain"
down_revision: Union[str, None] = "054_referrals_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "user_tutorials", "crystals_earned"):
        return
    op.add_column(
        "user_tutorials",
        sa.Column(
            "crystals_earned",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "user_tutorials", "crystals_earned"):
        op.drop_column("user_tutorials", "crystals_earned")
