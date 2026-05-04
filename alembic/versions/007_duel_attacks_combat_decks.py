"""Card attacks JSON for combat; per-user combat deck."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_duel_attacks_decks"
down_revision: Union[str, None] = "006_instance_public_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cards", sa.Column("attacks", sa.JSON(), nullable=True))
    op.create_table(
        "user_combat_decks",
        sa.Column("discord_user_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("instance_ids", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_combat_decks")
    op.drop_column("cards", "attacks")
