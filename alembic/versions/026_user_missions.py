"""Daily and weekly mission progress per user."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "026_user_missions"
down_revision: Union[str, None] = "025_instance_grade"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "user_missions"):
        return
    op.create_table(
        "user_missions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("period_type", sa.String(length=16), nullable=False),
        sa.Column("period_key", sa.String(length=16), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("set_code", sa.String(length=32), nullable=True),
        sa.Column("set_name", sa.String(length=256), nullable=True),
        sa.Column("card_id", sa.Integer(), nullable=True),
        sa.Column("card_name", sa.String(length=512), nullable=True),
        sa.Column("reward_crystals", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "discord_user_id",
            "period_type",
            "period_key",
            "slot",
            name="uq_user_missions_period_slot",
        ),
    )
    op.create_index(
        "ix_user_missions_discord_user_id",
        "user_missions",
        ["discord_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_missions_period_key",
        "user_missions",
        ["period_key"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "user_missions"):
        return
    op.drop_index("ix_user_missions_period_key", table_name="user_missions")
    op.drop_index("ix_user_missions_discord_user_id", table_name="user_missions")
    op.drop_table("user_missions")
