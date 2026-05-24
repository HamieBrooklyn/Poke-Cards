"""Drop unique constraint on card_auctions.instance_id so a card can be re-listed after its auction ends."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "021_auction_instance_not_unique"
down_revision: Union[str, None] = "020_auction_currency_bid_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        index_names = {i.get("name") for i in insp.get_indexes("card_auctions") if i.get("name")}
    except Exception:
        # Missing table or backend-specific inspector failure — nothing to migrate.
        index_names = set()

    if "uq_card_auctions_instance_id" in index_names:
        op.drop_index("uq_card_auctions_instance_id", table_name="card_auctions")
        index_names.discard("uq_card_auctions_instance_id")

    if "ix_card_auctions_instance_id" not in index_names:
        op.create_index("ix_card_auctions_instance_id", "card_auctions", ["instance_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        index_names = {i.get("name") for i in insp.get_indexes("card_auctions") if i.get("name")}
    except Exception:
        # Missing table or backend-specific inspector failure — nothing to migrate.
        index_names = set()

    if "ix_card_auctions_instance_id" in index_names:
        op.drop_index("ix_card_auctions_instance_id", table_name="card_auctions")
        index_names.discard("ix_card_auctions_instance_id")

    if "uq_card_auctions_instance_id" not in index_names:
        op.create_index(
            "uq_card_auctions_instance_id",
            "card_auctions",
            ["instance_id"],
            unique=True,
        )
