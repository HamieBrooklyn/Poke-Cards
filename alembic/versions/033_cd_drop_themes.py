"""Global and per-guild /cd drop themes."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "033_cd_drop_themes"
down_revision: Union[str, None] = "032_user_tutorial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cd_drop_themes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("theme_kind", sa.String(length=16), nullable=False),
        sa.Column("theme_value", sa.String(length=128), nullable=False),
        sa.Column("chance_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_discord_user_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", name="uq_cd_drop_themes_guild_id"),
    )
    op.create_index("ix_cd_drop_themes_guild_id", "cd_drop_themes", ["guild_id"])


def downgrade() -> None:
    op.drop_index("ix_cd_drop_themes_guild_id", table_name="cd_drop_themes")
    op.drop_table("cd_drop_themes")
