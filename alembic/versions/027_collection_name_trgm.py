"""PostgreSQL trigram index on card names for faster collection search (``LIKE %q%``)."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "027_collection_name_trgm"
down_revision: Union[str, None] = "026_user_missions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_cards_name_lower_trgm
        ON cards USING gin (lower(name) gin_trgm_ops)
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_cards_name_lower_trgm")
