"""Wishlist market alert preferences (auction/trade DMs + optional price caps)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "051_wishlist_market_alerts"
down_revision: Union[str, None] = "050_notify_missions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def _column_exists(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "user_web_preferences"):
        return
    if not _column_exists(bind, "user_web_preferences", "notify_wishlist_market"):
        op.add_column(
            "user_web_preferences",
            sa.Column(
                "notify_wishlist_market",
                sa.Boolean(),
                nullable=False,
                server_default="1",
            ),
        )
    if not _column_exists(bind, "user_web_preferences", "wishlist_alert_max_pokedollars"):
        op.add_column(
            "user_web_preferences",
            sa.Column("wishlist_alert_max_pokedollars", sa.Integer(), nullable=True),
        )
    if not _column_exists(bind, "user_web_preferences", "wishlist_alert_max_crystals"):
        op.add_column(
            "user_web_preferences",
            sa.Column("wishlist_alert_max_crystals", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "user_web_preferences"):
        return
    for col in (
        "wishlist_alert_max_crystals",
        "wishlist_alert_max_pokedollars",
        "notify_wishlist_market",
    ):
        if _column_exists(bind, "user_web_preferences", col):
            op.drop_column("user_web_preferences", col)
