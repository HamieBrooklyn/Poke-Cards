"""Add auction_obtained_at to user_card_instances for tracking auction wins."""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "046_auction_obtained"
down_revision: Union[str, None] = "045_fix_ancient_mew"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "user_card_instances",
        sa.Column("auction_obtained_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_card_instances", "auction_obtained_at")
