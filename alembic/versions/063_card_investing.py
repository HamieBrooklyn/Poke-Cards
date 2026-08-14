"""Card investing: live TCG quotes, daily history, player positions."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "063_card_investing"
down_revision: Union[str, None] = "062_grade_enchantment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "card_market_quotes" not in tables:
        op.create_table(
            "card_market_quotes",
            sa.Column("card_id", sa.Integer(), nullable=False),
            sa.Column("usd_cents", sa.Integer(), nullable=False),
            sa.Column("usd_low_cents", sa.Integer(), nullable=True),
            sa.Column("usd_mid_cents", sa.Integer(), nullable=True),
            sa.Column("usd_high_cents", sa.Integer(), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="tcgplayer"),
            sa.Column("tcgplayer_url", sa.String(length=512), nullable=True),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("card_id"),
        )
    if "card_market_history" not in tables:
        op.create_table(
            "card_market_history",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("card_id", sa.Integer(), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("usd_cents", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("card_id", "day", name="uq_card_market_history_day"),
        )
        op.create_index("ix_card_market_history_card_id", "card_market_history", ["card_id"])
    if "card_investments" not in tables:
        op.create_table(
            "card_investments",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
            sa.Column("card_id", sa.Integer(), nullable=False),
            sa.Column("pokedollars_in", sa.Integer(), nullable=False),
            sa.Column("entry_usd_cents", sa.Integer(), nullable=False),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_card_investments_discord_user_id", "card_investments", ["discord_user_id"])
        op.create_index("ix_card_investments_card_id", "card_investments", ["card_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "card_investments" in tables:
        op.drop_index("ix_card_investments_card_id", table_name="card_investments")
        op.drop_index("ix_card_investments_discord_user_id", table_name="card_investments")
        op.drop_table("card_investments")
    if "card_market_history" in tables:
        op.drop_index("ix_card_market_history_card_id", table_name="card_market_history")
        op.drop_table("card_market_history")
    if "card_market_quotes" in tables:
        op.drop_table("card_market_quotes")
