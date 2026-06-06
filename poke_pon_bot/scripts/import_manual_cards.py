"""CLI: import cards that are not in the Pokémon TCG API (e.g. Ancient Mew promos)."""

from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

from poke_pon_bot.config import load_settings
from poke_pon_bot.db.session import async_session_factory, create_engine_from_url
from poke_pon_bot.services.manual_cards import upsert_ancient_mew_cards


async def _run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    settings = load_settings(require_discord_token=False)
    engine = create_engine_from_url(settings.database_url)
    factory = async_session_factory(engine)
    try:
        n = await upsert_ancient_mew_cards(
            factory, web_public_url=settings.web_public_url
        )
        logging.getLogger(__name__).info("Done: %s Ancient Mew variant(s).", n)
    finally:
        await engine.dispose()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
