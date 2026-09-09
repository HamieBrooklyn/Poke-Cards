"""Separate TCG card definitions, lobbies, saved decks, and private history."""
import sqlalchemy as sa
from alembic import op

revision = "065_tcg_mode"
# Kept independent from the unrelated, still-unreleased vote-default migration.
# Alembic will track two heads until that migration is promoted and merged.
down_revision = "063_card_investing"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("tcg_card_definitions",
        sa.Column("tcg_card_id", sa.String(128), sa.ForeignKey("cards.tcg_card_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("data_version", sa.String(80), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("tcg_lobbies",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("code", sa.String(12), nullable=False),
        sa.Column("host_id", sa.BigInteger(), nullable=False),
        sa.Column("guest_id", sa.BigInteger()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("host_seen", sa.DateTime(timezone=True)),
        sa.Column("guest_seen", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_tcg_lobbies_code", "tcg_lobbies", ["code"], unique=True)
    op.create_index("ix_tcg_lobbies_host_id", "tcg_lobbies", ["host_id"])
    op.create_index("ix_tcg_lobbies_guest_id", "tcg_lobbies", ["guest_id"])
    op.create_table("tcg_saved_decks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_tcg_saved_decks_owner_id", "tcg_saved_decks", ["owner_id"])
    op.create_table("tcg_commands",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lobby_id", sa.String(32), sa.ForeignKey("tcg_lobbies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("command_id", sa.String(80), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.JSON(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("lobby_id", "actor_id", "command_id", name="uq_tcg_command"))
    op.create_index("ix_tcg_commands_lobby_id", "tcg_commands", ["lobby_id"])


def downgrade():
    op.drop_table("tcg_commands")
    op.drop_table("tcg_saved_decks")
    op.drop_table("tcg_lobbies")
    op.drop_table("tcg_card_definitions")
