"""CLI: sync Pokémon TCG cards from the public API into SQLite.

Run each layer of the YAML plan in order: ``all_sets`` (bulk discovery) → ``query``
(filtered Pokémon TCG `q=` expression) → ``sets`` (curated overrides). Layers are
additive; missing keys are skipped.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from poke_pon_bot.config import load_settings
from poke_pon_bot.db.session import async_session_factory, create_engine_from_url
from poke_pon_bot.services.catalog_sync import (
    fetch_all_set_ids,
    load_catalog_sync_plan_from_yaml,
    sync_curated_sets,
    sync_query,
)


async def _run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    settings = load_settings(require_discord_token=False)

    Path("data").mkdir(parents=True, exist_ok=True)

    LOG = logging.getLogger(__name__)
    plan = load_catalog_sync_plan_from_yaml(settings.card_sets_config)

    engine = create_engine_from_url(settings.database_url)
    factory = async_session_factory(engine)
    totals: dict[str, int] = {}
    try:
        if plan.get("all_sets"):
            LOG.info("Discovering every set from the Pokémon TCG API …")
            all_ids = await fetch_all_set_ids(api_key=settings.tcg_api_key)
            LOG.info("Discovered %s sets; importing every card from each.", len(all_ids))
            if all_ids:
                counts = await sync_curated_sets(
                    factory,
                    set_ids=all_ids,
                    api_key=settings.tcg_api_key,
                )
                totals.update(counts)

        if "query" in plan:
            LOG.info("Importing by query: %s", plan["query"])
            counts = await sync_query(
                factory,
                query=str(plan["query"]),
                api_key=settings.tcg_api_key,
                label="query",
            )
            totals.update(counts)

        if "sets" in plan:
            set_ids = list(plan["sets"])
            LOG.info("Importing curated sets: %s", ", ".join(set_ids))
            counts = await sync_curated_sets(
                factory,
                set_ids=set_ids,
                api_key=settings.tcg_api_key,
            )
            totals.update(counts)
    finally:
        await engine.dispose()

    for key, n in totals.items():
        LOG.info("%s: %s cards upserted", key, n)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
