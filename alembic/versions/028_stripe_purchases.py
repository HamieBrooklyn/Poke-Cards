"""Stripe Checkout purchase ledger (idempotent fulfillment)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "028_stripe_purchases"
down_revision: Union[str, None] = "027_collection_name_trgm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stripe_purchases",
        sa.Column("checkout_session_id", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("sku_id", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("amount_granted", sa.Integer(), nullable=False),
        sa.Column("stripe_price_id", sa.String(length=255), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_stripe_purchases_discord_user_id",
        "stripe_purchases",
        ["discord_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_stripe_purchases_discord_user_id", table_name="stripe_purchases")
    op.drop_table("stripe_purchases")
