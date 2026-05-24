"""Global and per-guild rarity luck boosts."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "034_rarity_luck_boosts"
down_revision: Union[str, None] = "033_cd_drop_themes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rarity_luck_boosts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("luck_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_discord_user_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", name="uq_rarity_luck_boosts_guild_id"),
    )
    op.create_index("ix_rarity_luck_boosts_guild_id", "rarity_luck_boosts", ["guild_id"])


def downgrade() -> None:
    op.drop_index("ix_rarity_luck_boosts_guild_id", table_name="rarity_luck_boosts")
    op.drop_table("rarity_luck_boosts")
