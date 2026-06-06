#!/usr/bin/env python3
"""Cancel all invited/active web duel sessions (maintenance / stuck duels).

Refunds escrow when locked but not paid out. Run against staging by default
when POKEPON_ENV=staging or pass --staging to load .env.staging.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poke_pon_bot.db.session import async_session_factory, create_engine_from_url
from poke_pon_bot.models.duel_session import (
    DUEL_STATUS_CANCELLED,
    DUEL_STATUS_EXPIRED,
    DuelSession,
)
from poke_pon_bot.services.web_duels import (
    _LIVE_STATUSES,
    _is_expired,
    expire_stale_duels,
    refund_escrow,
)
from poke_pon_bot.config import load_settings


async def cancel_all_live(*, dry_run: bool) -> int:
    settings = load_settings(require_discord_token=False)
    engine = create_engine_from_url(settings.database_url)
    factory = async_session_factory(engine)
    try:
        expired_ids = await expire_stale_duels(factory)
        if expired_ids:
            print(f"expire_stale_duels: {expired_ids}")

        async with factory() as db:
            rows = (
                await db.execute(
                    select(DuelSession).where(DuelSession.status.in_(_LIVE_STATUSES))
                )
            ).scalars().all()
            if not rows:
                print("No invited/active duels remain.")
                return 0

            for row in rows:
                label = (
                    f"#{row.id} {row.status} "
                    f"{row.initiator_id} vs {row.partner_id} "
                    f"expires={row.expires_at}"
                )
                if dry_run:
                    print(f"would cancel {label}")
                    continue
                if _is_expired(row):
                    row.status = DUEL_STATUS_EXPIRED
                    if row.escrow_locked_at and not row.escrow_paid_out_at:
                        await refund_escrow(db, row)
                    print(f"expired {label}")
                else:
                    row.status = DUEL_STATUS_CANCELLED
                    if row.escrow_locked_at and not row.escrow_paid_out_at:
                        await refund_escrow(db, row)
                    print(f"cancelled {label}")

            if not dry_run:
                await db.commit()
            return len(rows)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staging",
        action="store_true",
        help="Load .env.staging instead of .env",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List live duels without changing them",
    )
    args = parser.parse_args()
    env_file = ROOT / (".env.staging" if args.staging else ".env")
    if not env_file.is_file():
        print(f"Missing {env_file}", file=sys.stderr)
        sys.exit(1)
    load_dotenv(env_file)
    n = asyncio.run(cancel_all_live(dry_run=args.dry_run))
    if args.dry_run and n:
        print(f"{n} duel(s) would be cleared.")


if __name__ == "__main__":
    main()
