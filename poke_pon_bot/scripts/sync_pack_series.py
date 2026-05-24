"""CLI: reconcile ``card_series`` from the catalog + ``config/pack_series.v1.yaml``.

Run after importing new TCG sets (or when ``/packcat`` counts look stale) without
re-downloading every card.
"""

from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

from poke_pon_bot.config import load_settings
from poke_pon_bot.db.session import async_session_factory, create_engine_from_url
from poke_pon_bot.services.pack_series_loader import (
    cleanup_orphan_card_series_sets,
    prune_logo_only_pack_series,
    prune_orphan_series,
    sync_pack_series_from_catalog,
    upsert_pack_series,
)


async def _run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    settings = load_settings(require_discord_token=False)
    log = logging.getLogger(__name__)

    engine = create_engine_from_url(settings.database_url)
    factory = async_session_factory(engine)
    try:
        await cleanup_orphan_card_series_sets(factory)
        yaml_n, _ = await upsert_pack_series(factory)
        created = await sync_pack_series_from_catalog(factory)
        logo_deleted, logo_hidden = await prune_logo_only_pack_series(factory)
        deleted, deactivated = await prune_orphan_series(factory)
        log.info(
            "Done: %s YAML series, %s new catalog series, removed %s logo-only packs "
            "(%s hidden with owners), pruned %s orphan deleted / %s deactivated.",
            yaml_n,
            created,
            logo_deleted,
            logo_hidden,
            deleted,
            deactivated,
        )
    finally:
        await engine.dispose()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
