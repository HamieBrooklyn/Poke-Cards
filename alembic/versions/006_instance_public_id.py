"""Per-copy inventory Card ID (UUID hex) for display and future trades."""

from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = "006_instance_public_id"
down_revision: Union[str, None] = "005_evolves_from"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "public_id" in _table_columns(bind, "user_card_instances"):
        return

    op.add_column(
        "user_card_instances",
        sa.Column("public_id", sa.String(length=32), nullable=True),
    )

    for row in bind.execute(text("SELECT id FROM user_card_instances")).all():
        pid = uuid.uuid4().hex
        bind.execute(
            text("UPDATE user_card_instances SET public_id = :p WHERE id = :i"),
            {"p": pid, "i": row[0]},
        )
    with op.batch_alter_table("user_card_instances") as batch:
        batch.alter_column(
            "public_id",
            existing_type=sa.String(length=32),
            nullable=False,
        )
    op.create_index(
        "ix_user_card_instances_public_id",
        "user_card_instances",
        ["public_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_user_card_instances_public_id", table_name="user_card_instances")
    op.drop_column("user_card_instances", "public_id")
