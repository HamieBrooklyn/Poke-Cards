"""Crystal sinks: auction spotlight, leaderboard frames, drop reroll flag."""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "052_crystal_sinks"
down_revision: Union[str, None] = "051_wishlist_market_alerts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def _column_exists(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "card_auctions") and not _column_exists(bind, "card_auctions", "spotlight_until"):
        op.add_column(
            "card_auctions",
            sa.Column("spotlight_until", sa.DateTime(timezone=True), nullable=True),
        )
    if _table_exists(bind, "user_web_preferences"):
        if not _column_exists(bind, "user_web_preferences", "leaderboard_frame"):
            op.add_column(
                "user_web_preferences",
                sa.Column("leaderboard_frame", sa.String(32), nullable=True),
            )
        if not _column_exists(bind, "user_web_preferences", "unlocked_leaderboard_frames"):
            op.add_column(
                "user_web_preferences",
                sa.Column(
                    "unlocked_leaderboard_frames",
                    sa.JSON(),
                    nullable=False,
                    server_default=json.dumps([]),
                ),
            )
    if _table_exists(bind, "pending_channel_drops") and not _column_exists(
        bind, "pending_channel_drops", "reroll_used"
    ):
        op.add_column(
            "pending_channel_drops",
            sa.Column("reroll_used", sa.Boolean(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table, col in (
        ("card_auctions", "spotlight_until"),
        ("user_web_preferences", "leaderboard_frame"),
        ("user_web_preferences", "unlocked_leaderboard_frames"),
        ("pending_channel_drops", "reroll_used"),
    ):
        if _table_exists(bind, table) and _column_exists(bind, table, col):
            op.drop_column(table, col)
