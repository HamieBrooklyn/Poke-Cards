"""Per-user card wishlists for drop/pack notifications."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018_user_wishlists"
down_revision: Union[str, None] = "017_pack_instance_guild_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_wishlists",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("discord_user_id", "card_id", name="uq_user_wishlists_user_card"),
    )
    op.create_index("ix_user_wishlists_discord_user_id", "user_wishlists", ["discord_user_id"])
    op.create_index("ix_user_wishlists_card_id", "user_wishlists", ["card_id"])


def downgrade() -> None:
    op.drop_index("ix_user_wishlists_card_id", table_name="user_wishlists")
    op.drop_index("ix_user_wishlists_discord_user_id", table_name="user_wishlists")
    op.drop_table("user_wishlists")
