"""Persist active /cd channel drops across bot restarts."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "042_pending_channel_drops"
down_revision: Union[str, None] = "041_restore_cd_drop_weights"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_channel_drops",
        sa.Column("message_id", sa.BigInteger(), primary_key=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("issuer_id", sa.BigInteger(), nullable=False),
        sa.Column("opener_mention", sa.String(256), nullable=False),
        sa.Column("card_ids", sa.JSON(), nullable=False),
        sa.Column("deadline_unix", sa.Integer(), nullable=False),
        sa.Column("claim_seconds", sa.Integer(), nullable=False),
        sa.Column("max_grabs_per_user", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("private_pack", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("mission_block", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_prefix", sa.Text(), nullable=False, server_default=""),
        sa.Column("slot_claimer", sa.JSON(), nullable=False),
        sa.Column("slot_public_ids", sa.JSON(), nullable=False),
        sa.Column("finished", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_pending_channel_drops_channel_id",
        "pending_channel_drops",
        ["channel_id"],
    )
    op.create_index(
        "ix_pending_channel_drops_guild_id",
        "pending_channel_drops",
        ["guild_id"],
    )
    op.create_index(
        "ix_pending_channel_drops_issuer_id",
        "pending_channel_drops",
        ["issuer_id"],
    )
    op.create_index(
        "ix_pending_channel_drops_deadline_unix",
        "pending_channel_drops",
        ["deadline_unix"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_channel_drops_deadline_unix", table_name="pending_channel_drops")
    op.drop_index("ix_pending_channel_drops_issuer_id", table_name="pending_channel_drops")
    op.drop_index("ix_pending_channel_drops_guild_id", table_name="pending_channel_drops")
    op.drop_index("ix_pending_channel_drops_channel_id", table_name="pending_channel_drops")
    op.drop_table("pending_channel_drops")
