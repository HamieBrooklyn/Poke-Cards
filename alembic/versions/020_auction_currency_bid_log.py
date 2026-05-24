"""Auction bid currency (₽ / 💎) + bid history log for web detail."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "020_auction_currency_bid_log"
down_revision: Union[str, None] = "019_pending_trades_crystals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("card_auctions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "bid_currency",
                sa.String(length=16),
                nullable=False,
                server_default="pokedollars",
            ),
        )

    op.create_table(
        "auction_bids",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("auction_id", sa.Integer(), sa.ForeignKey("card_auctions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bidder_discord_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_auction_bids_auction_id", "auction_bids", ["auction_id"])
    op.create_index("ix_auction_bids_created_at", "auction_bids", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_auction_bids_created_at", table_name="auction_bids")
    op.drop_index("ix_auction_bids_auction_id", table_name="auction_bids")
    op.drop_table("auction_bids")
    with op.batch_alter_table("card_auctions") as batch_op:
        batch_op.drop_column("bid_currency")
