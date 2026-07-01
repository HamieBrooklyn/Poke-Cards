"""Catalog announcement card IDs + link set code for filtered Pokédex."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "061_catalog_news_card_ids"
down_revision: Union[str, None] = "060_catalog_announcements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "catalog_announcements",
        sa.Column("card_ids_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "catalog_announcements",
        sa.Column("catalog_set_code", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("catalog_announcements", "catalog_set_code")
    op.drop_column("catalog_announcements", "card_ids_json")
