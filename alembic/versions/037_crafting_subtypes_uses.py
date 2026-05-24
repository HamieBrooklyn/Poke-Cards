"""Crafting: card subtypes + trainer craft uses on owned copies."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "037_crafting_subtypes_uses"
down_revision: Union[str, None] = "036_user_invite_codes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cards", sa.Column("tcg_subtypes", sa.JSON(), nullable=True))
    op.add_column(
        "user_card_instances",
        sa.Column("craft_uses_remaining", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_card_instances", "craft_uses_remaining")
    op.drop_column("cards", "tcg_subtypes")
