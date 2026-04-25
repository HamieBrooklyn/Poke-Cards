"""Async engine and session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine_from_url(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url,
        echo=False,
        future=True,
    )


def async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
