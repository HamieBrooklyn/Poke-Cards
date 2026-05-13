"""Track which guild a pack was bought in so /packcat can scope server vs global."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "017_pack_instance_guild_id"
down_revision: Union[str, None] = "016_packs_and_crystals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("user_pack_instances") as batch_op:
        batch_op.add_column(sa.Column("guild_id", sa.BigInteger(), nullable=True))
    op.create_index(
        "ix_user_pack_instances_guild_id",
        "user_pack_instances",
        ["guild_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_pack_instances_guild_id", table_name="user_pack_instances")
    with op.batch_alter_table("user_pack_instances") as batch_op:
        batch_op.drop_column("guild_id")
