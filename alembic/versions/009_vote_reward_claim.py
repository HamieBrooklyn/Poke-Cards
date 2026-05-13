"""Vote reward claim timestamp.

Adds a persistent cooldown for /pv claims.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009_vote_reward_claim"
down_revision: Union[str, None] = "008_pending_trades"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_pokedollars", sa.Column("last_vote_claim_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("user_pokedollars", "last_vote_claim_at")

