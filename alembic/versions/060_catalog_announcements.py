"""Catalog announcements for new TCG sets on web + Discord."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "060_catalog_announcements"
down_revision: Union[str, None] = "059_scheduled_game_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_announcements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("set_code", sa.String(length=64), nullable=False),
        sa.Column("set_name", sa.String(length=256), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("new_card_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_cards_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("pokedex_url", sa.String(length=512), nullable=True),
        sa.Column("packs_url", sa.String(length=512), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("discord_message_id", sa.BigInteger(), nullable=True),
        sa.Column("discord_channel_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_catalog_announcements_published_at",
        "catalog_announcements",
        ["published_at"],
    )
    op.create_index(
        "ix_catalog_announcements_set_code",
        "catalog_announcements",
        ["set_code"],
    )


def downgrade() -> None:
    op.drop_index("ix_catalog_announcements_set_code", table_name="catalog_announcements")
    op.drop_index("ix_catalog_announcements_published_at", table_name="catalog_announcements")
    op.drop_table("catalog_announcements")
