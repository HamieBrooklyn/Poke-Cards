"""Alembic migration environment — uses sync sqlite URL derived from DATABASE_URL."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from poke_pon_bot.db.base import Base
import poke_pon_bot.models  # noqa: F401 — register models on metadata

load_dotenv()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_sync_database_url() -> str:
    # Priority: explicit ``sqlalchemy.url`` set on the alembic Config (e.g. when the bot
    # boots and calls ``command.upgrade`` programmatically) → ``DATABASE_URL`` env var →
    # hardcoded local sqlite default. Without this fallback chain, in-process invocations
    # silently fall back to the env var and skip the upgrade.
    cfg_url = (config.get_main_option("sqlalchemy.url") or "").strip()
    url = cfg_url or os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/poke_cards.db").strip()
    if "+aiosqlite" in url:
        return url.replace("sqlite+aiosqlite", "sqlite", 1)
    return url


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = get_sync_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": get_sync_database_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
