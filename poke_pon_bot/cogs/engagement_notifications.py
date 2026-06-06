"""Background task: daily, vote, and card-drop engagement reminders."""

from __future__ import annotations

import logging

from discord.ext import commands, tasks
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.engagement_reminders import process_engagement_reminders

_LOG = logging.getLogger(__name__)


class EngagementNotificationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self._engagement_loop.start()

    async def cog_unload(self) -> None:
        self._engagement_loop.cancel()

    @tasks.loop(hours=1)
    async def _engagement_loop(self) -> None:
        settings = self.bot.settings
        try:
            await process_engagement_reminders(
                self.bot,
                self.bot.async_session_factory,
                drop_cooldown_base_seconds=settings.drop_cooldown_base_seconds,
                drop_cooldown_premium_seconds=settings.drop_cooldown_premium_seconds,
                drop_boost_test_user_ids=settings.drop_boost_test_user_ids,
                topgg_vote_url=settings.topgg_vote_url,
                web_frontend_url=getattr(settings, "web_frontend_url", None),
            )
        except SQLAlchemyError:
            _LOG.exception("engagement reminder loop failed")

    @_engagement_loop.before_loop
    async def _engagement_before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EngagementNotificationsCog(bot))
