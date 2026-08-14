"""Background catalog sync + new-set announcements."""

from __future__ import annotations

import logging

from discord.ext import commands, tasks

from poke_pon_bot.services.catalog_news import _site_origin, run_catalog_news_sync

_LOG = logging.getLogger(__name__)


class CatalogNewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if self.bot.settings.catalog_news_enabled:
            hours = max(1.0, float(self.bot.settings.catalog_news_interval_hours))
            self._catalog_news_loop.change_interval(hours=hours)
            self._catalog_news_loop.start()

    async def cog_unload(self) -> None:
        self._catalog_news_loop.cancel()

    async def run_once(self) -> list[str]:
        settings = self.bot.settings
        result = await run_catalog_news_sync(
            self.bot,
            self.bot.async_session_factory,
            api_key=settings.tcg_api_key,
            site_origin=_site_origin(settings.web_frontend_url),
            lookback_days=settings.catalog_news_lookback_days,
            min_cards_existing_set=settings.catalog_news_min_cards_existing_set,
            max_sets_per_run=settings.catalog_news_max_sets_per_run,
            discord_channel_id=settings.catalog_news_channel_id,
            post_discord=settings.catalog_news_channel_id is not None,
        )
        return result.messages

    @tasks.loop(hours=24)
    async def _catalog_news_loop(self) -> None:
        try:
            messages = await self.run_once()
            for msg in messages:
                _LOG.info("catalog news: %s", msg)
        except Exception:
            _LOG.exception("catalog news sync failed")

    @_catalog_news_loop.before_loop
    async def _catalog_news_before(self) -> None:
        await self.bot.wait_until_ready()
        # Stagger first run so startup pack sync / gateway login finish first.
        import asyncio

        await asyncio.sleep(120)
        try:
            messages = await self.run_once()
            for msg in messages:
                _LOG.info("catalog news startup: %s", msg)
        except Exception:
            _LOG.exception("catalog news startup sync failed")


async def setup(bot: commands.Bot) -> None:
    if bot.settings.catalog_news_enabled:
        await bot.add_cog(CatalogNewsCog(bot))
