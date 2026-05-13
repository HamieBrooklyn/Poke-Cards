"""Add ``cards.tcg_types`` for Pokémon TCG energy / defending types (duel effectiveness)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012_card_tcg_types"
down_revision: Union[str, None] = "011_auction_duration_bids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cards", sa.Column("tcg_types", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("cards", "tcg_types")
