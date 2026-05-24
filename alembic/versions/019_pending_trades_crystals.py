"""Pending trades: optional Crystals on each side."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "019_pending_trades_crystals"
down_revision: Union[str, None] = "018_user_card_wishlist"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("pending_trades") as batch_op:
        batch_op.add_column(
            sa.Column("give_crystals", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("receive_crystals", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("pending_trades") as batch_op:
        batch_op.drop_column("receive_crystals")
        batch_op.drop_column("give_crystals")
