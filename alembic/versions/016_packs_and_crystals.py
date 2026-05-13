"""Packs feature: card_series + card_series_sets + user_crystals + user_pack_instances."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "016_packs_and_crystals"
down_revision: Union[str, None] = "015_card_auctions_guild_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "card_series",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("crystal_price", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("pack_art_url", sa.Text(), nullable=True),
        sa.Column("cards_per_pack", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("code_cards_per_pack", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_card_series_code", "card_series", ["code"], unique=True)

    op.create_table(
        "card_series_sets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("set_code", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["series_id"], ["card_series.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("series_id", "set_code", name="uq_card_series_sets_series_setcode"),
    )
    op.create_index("ix_card_series_sets_series_id", "card_series_sets", ["series_id"])
    op.create_index("ix_card_series_sets_set_code", "card_series_sets", ["set_code"])

    op.create_table(
        "user_crystals",
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_vote_claim_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rewarded_topgg_vote_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("discord_user_id"),
    )

    op.create_table(
        "user_pack_instances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="purchase_pokedollars",
        ),
        sa.Column(
            "obtained_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["series_id"], ["card_series.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_user_pack_instances_public_id",
        "user_pack_instances",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        "ix_user_pack_instances_discord_user_id",
        "user_pack_instances",
        ["discord_user_id"],
    )
    op.create_index(
        "ix_user_pack_instances_series_id",
        "user_pack_instances",
        ["series_id"],
    )
    op.create_index(
        "ix_user_pack_instances_opened_at",
        "user_pack_instances",
        ["opened_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_pack_instances_opened_at", table_name="user_pack_instances")
    op.drop_index("ix_user_pack_instances_series_id", table_name="user_pack_instances")
    op.drop_index("ix_user_pack_instances_discord_user_id", table_name="user_pack_instances")
    op.drop_index("ix_user_pack_instances_public_id", table_name="user_pack_instances")
    op.drop_table("user_pack_instances")
    op.drop_table("user_crystals")
    op.drop_index("ix_card_series_sets_set_code", table_name="card_series_sets")
    op.drop_index("ix_card_series_sets_series_id", table_name="card_series_sets")
    op.drop_table("card_series_sets")
    op.drop_index("ix_card_series_code", table_name="card_series")
    op.drop_table("card_series")
