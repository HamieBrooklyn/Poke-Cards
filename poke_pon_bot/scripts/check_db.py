"""Verify DATABASE_URL connects and report Alembic revision (if any)."""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from poke_pon_bot.db.urls import async_database_url, sync_database_url


async def _async_ping(url: str) -> None:
    engine = create_async_engine(async_database_url(url), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT 1 AS ok"))).one()
            print(f"async OK: {row.ok}")
    finally:
        await engine.dispose()


def _sync_revision(url: str) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(sync_database_url(url), pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            try:
                rev = conn.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1"),
                ).scalar_one_or_none()
            except Exception as exc:
                print(f"alembic_version: not present yet ({exc.__class__.__name__})")
                return
            print(f"alembic revision: {rev or '(empty)'}")
    finally:
        engine.dispose()


def main() -> None:
    load_dotenv()
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("Set DATABASE_URL in .env first.", file=sys.stderr)
        sys.exit(1)
    print(f"target: {url.split('@')[-1] if '@' in url else url}")
    asyncio.run(_async_ping(url))
    _sync_revision(url)


if __name__ == "__main__":
    main()
