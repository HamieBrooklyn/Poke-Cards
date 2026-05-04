"""Pending P2P trades (cards + Pokedollars)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_pending_trades"
down_revision: Union[str, None] = "007_duel_attacks_decks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_trades",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("initiator_id", sa.BigInteger(), nullable=False),
        sa.Column("partner_id", sa.BigInteger(), nullable=False),
        sa.Column("give_instance_ids", sa.JSON(), nullable=False),
        sa.Column("give_pokedollars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("receive_instance_ids", sa.JSON(), nullable=False),
        sa.Column("receive_pokedollars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pending_trades_initiator_id", "pending_trades", ["initiator_id"])
    op.create_index("ix_pending_trades_partner_id", "pending_trades", ["partner_id"])


def downgrade() -> None:
    op.drop_index("ix_pending_trades_partner_id", table_name="pending_trades")
    op.drop_index("ix_pending_trades_initiator_id", table_name="pending_trades")
    op.drop_table("pending_trades")
