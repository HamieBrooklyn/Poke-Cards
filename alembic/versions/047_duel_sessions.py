"""Add duel_sessions for website PvP duels."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "047_duel_sessions"
down_revision: Union[str, None] = "046_auction_obtained"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "duel_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("initiator_id", sa.BigInteger(), nullable=False),
        sa.Column("partner_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="invited"),
        sa.Column("bet_currency", sa.String(16), nullable=False, server_default="pokedollars"),
        sa.Column("bet_amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starting_player_id", sa.BigInteger(), nullable=True),
        sa.Column("escrow_currency", sa.String(16), nullable=True),
        sa.Column("escrow_amount_each", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("escrow_locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escrow_paid_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("winner_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_duel_sessions_initiator_id", "duel_sessions", ["initiator_id"])
    op.create_index("ix_duel_sessions_partner_id", "duel_sessions", ["partner_id"])
    op.create_index("ix_duel_sessions_status", "duel_sessions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_duel_sessions_status", table_name="duel_sessions")
    op.drop_index("ix_duel_sessions_partner_id", table_name="duel_sessions")
    op.drop_index("ix_duel_sessions_initiator_id", table_name="duel_sessions")
    op.drop_table("duel_sessions")

