"""Initial catalog, rarity tiers, drop weights, inventory."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rarity_classes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("code", name="uq_rarity_classes_code"),
    )
    op.create_index("ix_rarity_classes_code", "rarity_classes", ["code"], unique=False)

    op.create_table(
        "drop_tables",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False, server_default=""),
        sa.UniqueConstraint("code", name="uq_drop_tables_code"),
    )
    op.create_index("ix_drop_tables_code", "drop_tables", ["code"], unique=False)

    op.create_table(
        "tcg_rarity_mappings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tcg_rarity", sa.String(length=256), nullable=False),
        sa.Column("rarity_class_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tcg_rarity", name="uq_tcg_rarity_mappings_tcg_rarity"),
        sa.ForeignKeyConstraint(["rarity_class_id"], ["rarity_classes.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_tcg_rarity_mappings_tcg_rarity",
        "tcg_rarity_mappings",
        ["tcg_rarity"],
        unique=False,
    )

    op.create_table(
        "drop_weights",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("drop_table_id", sa.Integer(), nullable=False),
        sa.Column("rarity_class_id", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["drop_table_id"], ["drop_tables.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rarity_class_id"], ["rarity_classes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "drop_table_id",
            "rarity_class_id",
            name="uq_drop_weights_table_rarity",
        ),
    )
    op.create_index("ix_drop_weights_drop_table_id", "drop_weights", ["drop_table_id"], unique=False)
    op.create_index(
        "ix_drop_weights_rarity_class_id",
        "drop_weights",
        ["rarity_class_id"],
        unique=False,
    )

    op.create_table(
        "cards",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tcg_card_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("set_code", sa.String(length=32), nullable=False),
        sa.Column("set_name", sa.String(length=256), nullable=False),
        sa.Column("collector_number", sa.String(length=32), nullable=False),
        sa.Column("tcg_rarity", sa.String(length=256), nullable=True),
        sa.Column("image_small_url", sa.Text(), nullable=False),
        sa.Column("image_large_url", sa.Text(), nullable=False),
        sa.Column("supertype", sa.String(length=64), nullable=True),
        sa.Column("hp", sa.String(length=16), nullable=True),
        sa.Column("dex_numbers", sa.JSON(), nullable=True),
        sa.Column("rarity_class_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tcg_card_id", name="uq_cards_tcg_card_id"),
        sa.ForeignKeyConstraint(["rarity_class_id"], ["rarity_classes.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_cards_tcg_card_id", "cards", ["tcg_card_id"], unique=False)
    op.create_index("ix_cards_set_code", "cards", ["set_code"], unique=False)
    op.create_index("ix_cards_rarity_class_id", "cards", ["rarity_class_id"], unique=False)

    op.create_table(
        "user_card_instances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column(
            "obtained_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="drop"),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_user_card_instances_discord_user_id",
        "user_card_instances",
        ["discord_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_card_instances_card_id",
        "user_card_instances",
        ["card_id"],
        unique=False,
    )

    op.execute(
        sa.text("""
        INSERT INTO rarity_classes (id, code, display_name, sort_order) VALUES
        (1, 'common', 'Common', 1),
        (2, 'uncommon', 'Uncommon', 2),
        (3, 'rare', 'Rare', 3),
        (4, 'rare_holo', 'Rare Holo', 4),
        (5, 'ultra_rare', 'Ultra Rare', 5),
        (6, 'double_rare', 'Double Rare', 6),
        (7, 'illustration_rare', 'Illustration Rare', 7),
        (8, 'special_rare', 'Special Rare', 8),
        (9, 'hyper_rare', 'Hyper Rare', 9),
        (10, 'chase', 'Chase', 10)
        """)
    )

    op.execute(
        sa.text("""
        INSERT INTO drop_tables (id, code, display_name) VALUES
        (1, 'default', 'Default pool')
        """)
    )

    op.execute(
        sa.text("""
        INSERT INTO drop_weights (drop_table_id, rarity_class_id, weight) VALUES
        (1, 1, 100000),
        (1, 2, 60000),
        (1, 3, 35000),
        (1, 4, 8000),
        (1, 5, 4000),
        (1, 6, 2500),
        (1, 7, 1200),
        (1, 8, 400),
        (1, 9, 40),
        (1, 10, 2)
        """)
    )


def downgrade() -> None:
    op.drop_index("ix_user_card_instances_card_id", table_name="user_card_instances")
    op.drop_index("ix_user_card_instances_discord_user_id", table_name="user_card_instances")
    op.drop_table("user_card_instances")

    op.drop_index("ix_cards_rarity_class_id", table_name="cards")
    op.drop_index("ix_cards_set_code", table_name="cards")
    op.drop_index("ix_cards_tcg_card_id", table_name="cards")
    op.drop_table("cards")

    op.drop_index("ix_drop_weights_rarity_class_id", table_name="drop_weights")
    op.drop_index("ix_drop_weights_drop_table_id", table_name="drop_weights")
    op.drop_table("drop_weights")

    op.drop_index("ix_tcg_rarity_mappings_tcg_rarity", table_name="tcg_rarity_mappings")
    op.drop_table("tcg_rarity_mappings")

    op.drop_index("ix_drop_tables_code", table_name="drop_tables")
    op.drop_table("drop_tables")

    op.drop_index("ix_rarity_classes_code", table_name="rarity_classes")
    op.drop_table("rarity_classes")
