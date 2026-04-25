"""CLI: sync Pokémon TCG cards from the public API into SQLite."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from poke_pon_bot.config import load_settings
from poke_pon_bot.db.session import async_session_factory, create_engine_from_url
from poke_pon_bot.services.catalog_sync import load_set_ids_from_yaml, sync_curated_sets


async def _run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    settings = load_settings(require_discord_token=False)

    Path("data").mkdir(parents=True, exist_ok=True)

    set_ids = load_set_ids_from_yaml(settings.card_sets_config)
    LOG = logging.getLogger(__name__)
    LOG.info("Importing sets: %s", ", ".join(set_ids))

    engine = create_engine_from_url(settings.database_url)
    factory = async_session_factory(engine)
    try:
        counts = await sync_curated_sets(
            factory,
            set_ids=set_ids,
            api_key=settings.tcg_api_key,
        )
    finally:
        await engine.dispose()

    for sid, n in counts.items():
        LOG.info("Set %s: %s cards upserted", sid, n)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
