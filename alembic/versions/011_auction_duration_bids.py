"""Auction end time, high bid tracking, settlement statuses."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011_auction_duration_bids"
down_revision: Union[str, None] = "010_card_auctions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("card_auctions") as batch_op:
        batch_op.add_column(sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("high_bidder_discord_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("high_bid_pokedollars", sa.Integer(), nullable=True))

    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        op.execute(
            sa.text(
                "UPDATE card_auctions SET ends_at = datetime(created_at, '+86400 seconds') WHERE ends_at IS NULL",
            ),
        )
    else:
        op.execute(
            sa.text(
                "UPDATE card_auctions SET ends_at = created_at + interval '1 day' WHERE ends_at IS NULL",
            ),
        )

    with op.batch_alter_table("card_auctions") as batch_op:
        batch_op.alter_column(
            "ends_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )

    op.create_index("ix_card_auctions_status_ends_at", "card_auctions", ["status", "ends_at"])


def downgrade() -> None:
    op.drop_index("ix_card_auctions_status_ends_at", table_name="card_auctions")
    with op.batch_alter_table("card_auctions") as batch_op:
        batch_op.drop_column("high_bid_pokedollars")
        batch_op.drop_column("high_bidder_discord_id")
        batch_op.drop_column("ends_at")
