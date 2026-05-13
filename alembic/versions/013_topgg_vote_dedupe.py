"""Store Top.gg vote ``created_at`` for last paid reward (one claim per vote)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013_topgg_vote_dedupe"
down_revision: Union[str, None] = "012_card_tcg_types"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_pokedollars",
        sa.Column("last_rewarded_topgg_vote_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_pokedollars", "last_rewarded_topgg_vote_at")
