"""Background task: global weekend rarity luck (matches official Discord event window)."""

from __future__ import annotations

import logging

from discord.ext import commands, tasks
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.weekend_luck_schedule import sync_weekend_luck_boost

_LOG = logging.getLogger(__name__)


class WeekendLuckCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_active: bool | None = None

    async def cog_load(self) -> None:
        if self.bot.settings.weekend_luck_enabled:
            self._weekend_luck_loop.start()

    async def cog_unload(self) -> None:
        self._weekend_luck_loop.cancel()

    @tasks.loop(minutes=1)
    async def _weekend_luck_loop(self) -> None:
        settings = self.bot.settings
        try:
            from poke_pon_bot.services.weekend_luck_schedule import is_weekend_luck_window_active

            active = is_weekend_luck_window_active(timezone=settings.weekend_luck_timezone)
            if active == self._last_active:
                return
            msg = await sync_weekend_luck_boost(
                self.bot.async_session_factory,
                enabled=settings.weekend_luck_enabled,
                luck_percent=settings.weekend_luck_percent,
                timezone=settings.weekend_luck_timezone,
            )
            self._last_active = active
            if msg:
                _LOG.info(msg)
        except SQLAlchemyError:
            _LOG.exception("weekend luck sync failed")

    @_weekend_luck_loop.before_loop
    async def _weekend_luck_before(self) -> None:
        await self.bot.wait_until_ready()
        # Align DB with current window on startup (e.g. bot restarted mid-event).
        try:
            msg = await sync_weekend_luck_boost(
                self.bot.async_session_factory,
                enabled=self.bot.settings.weekend_luck_enabled,
                luck_percent=self.bot.settings.weekend_luck_percent,
                timezone=self.bot.settings.weekend_luck_timezone,
            )
            if msg:
                _LOG.info(msg)
            from poke_pon_bot.services.weekend_luck_schedule import (
                is_weekend_luck_window_active,
            )

            self._last_active = is_weekend_luck_window_active(
                timezone=self.bot.settings.weekend_luck_timezone
            )
        except SQLAlchemyError:
            _LOG.exception("weekend luck startup sync failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WeekendLuckCog(bot))
