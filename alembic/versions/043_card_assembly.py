"""Card assembly groups and piece slots (V-UNION, BREAK halves, etc.)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "043_card_assembly"
down_revision: Union[str, None] = "042_pending_channel_drops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "card_assembly_groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("result_card_id", sa.Integer(), nullable=False),
        sa.Column("piece_count", sa.Integer(), nullable=False),
        sa.Column("layout", sa.String(length=32), nullable=False),
        sa.Column("orientation", sa.String(length=16), nullable=False, server_default="portrait"),
        sa.ForeignKeyConstraint(["result_card_id"], ["cards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_card_assembly_groups_code", "card_assembly_groups", ["code"], unique=True)
    op.create_index(
        "ix_card_assembly_groups_result_card_id",
        "card_assembly_groups",
        ["result_card_id"],
        unique=False,
    )

    op.create_table(
        "card_assembly_pieces",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("rotation_deg", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("grid_col", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("grid_row", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["card_assembly_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "card_id", name="uq_assembly_piece_group_card"),
        sa.UniqueConstraint("group_id", "slot_index", name="uq_assembly_piece_group_slot"),
    )
    op.create_index("ix_card_assembly_pieces_group_id", "card_assembly_pieces", ["group_id"])
    op.create_index("ix_card_assembly_pieces_card_id", "card_assembly_pieces", ["card_id"])


def downgrade() -> None:
    op.drop_table("card_assembly_pieces")
    op.drop_table("card_assembly_groups")
