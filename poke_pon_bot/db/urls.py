"""Normalize ``DATABASE_URL`` for async app engines vs sync Alembic migrations."""

from __future__ import annotations


def sync_database_url(database_url: str) -> str:
    """Return a sync SQLAlchemy URL suitable for Alembic and other blocking drivers."""
    url = database_url.strip()
    if "+aiosqlite" in url:
        return url.replace("sqlite+aiosqlite", "sqlite", 1)
    if "+asyncpg" in url:
        return url.replace("postgresql+asyncpg", "postgresql+psycopg", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


def async_database_url(database_url: str) -> str:
    """Return an async SQLAlchemy URL for ``create_async_engine``."""
    url = database_url.strip()
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url
