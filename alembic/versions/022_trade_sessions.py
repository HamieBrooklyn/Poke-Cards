"""Interactive web trade sessions and known-users cache for username search."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "022_trade_sessions"
down_revision: Union[str, None] = "021_auction_instance_not_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("initiator_id", sa.BigInteger(), nullable=False),
        sa.Column("partner_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="invited"),
        sa.Column("initiator_card_ids", sa.JSON(), nullable=False),
        sa.Column("initiator_pokedollars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("initiator_crystals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("initiator_ready", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("partner_card_ids", sa.JSON(), nullable=False),
        sa.Column("partner_pokedollars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("partner_crystals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("partner_ready", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trade_sessions_initiator_id", "trade_sessions", ["initiator_id"])
    op.create_index("ix_trade_sessions_partner_id", "trade_sessions", ["partner_id"])
    op.create_index("ix_trade_sessions_status", "trade_sessions", ["status"])

    op.create_table(
        "known_users",
        sa.Column("discord_id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("global_name", sa.String(64), nullable=True),
        sa.Column("avatar_url", sa.String(256), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_known_users_username", "known_users", ["username"])


def downgrade() -> None:
    op.drop_index("ix_known_users_username", table_name="known_users")
    op.drop_table("known_users")
    op.drop_index("ix_trade_sessions_status", table_name="trade_sessions")
    op.drop_index("ix_trade_sessions_partner_id", table_name="trade_sessions")
    op.drop_index("ix_trade_sessions_initiator_id", table_name="trade_sessions")
    op.drop_table("trade_sessions")
