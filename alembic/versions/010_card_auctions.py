"""Active card listings (auction MVP — price display + search)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_card_auctions"
down_revision: Union[str, None] = "009_vote_reward_claim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "card_auctions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("seller_discord_id", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.Integer(), nullable=False),
        sa.Column("price_pokedollars", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["instance_id"], ["user_card_instances.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_card_auctions_seller_discord_id", "card_auctions", ["seller_discord_id"])
    op.create_index("ix_card_auctions_status_created", "card_auctions", ["status", "created_at"])
    op.create_index("uq_card_auctions_instance_id", "card_auctions", ["instance_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_card_auctions_instance_id", table_name="card_auctions")
    op.drop_index("ix_card_auctions_status_created", table_name="card_auctions")
    op.drop_index("ix_card_auctions_seller_discord_id", table_name="card_auctions")
    op.drop_table("card_auctions")
