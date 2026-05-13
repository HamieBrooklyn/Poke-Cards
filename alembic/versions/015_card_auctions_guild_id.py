"""Track which guild a listing came from so /auction search can scope server vs global."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015_card_auctions_guild_id"
down_revision: Union[str, None] = "014_topgg_processed_votes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("card_auctions") as batch_op:
        batch_op.add_column(sa.Column("guild_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_card_auctions_guild_id", "card_auctions", ["guild_id"])


def downgrade() -> None:
    op.drop_index("ix_card_auctions_guild_id", table_name="card_auctions")
    with op.batch_alter_table("card_auctions") as batch_op:
        batch_op.drop_column("guild_id")
