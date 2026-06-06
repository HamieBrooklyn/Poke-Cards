"""Background task: sync scheduled game events (luck boost, etc.)."""

from __future__ import annotations

import logging

from discord.ext import commands, tasks
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.event_scheduler import effects_fingerprint, resolve_active_effects
from poke_pon_bot.services.weekend_luck_schedule import sync_weekend_luck_boost

_LOG = logging.getLogger(__name__)


class GameEventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_fingerprint: str | None = None

    async def cog_load(self) -> None:
        self._game_events_loop.start()

    async def cog_unload(self) -> None:
        self._game_events_loop.cancel()

    async def _sync_once(self) -> None:
        settings = self.bot.settings
        async with self.bot.async_session_factory() as session:
            effects = await resolve_active_effects(
                session,
                weekend_luck_enabled=settings.weekend_luck_enabled,
                weekend_luck_percent=settings.weekend_luck_percent,
                weekend_luck_timezone=settings.weekend_luck_timezone,
            )
        fp = effects_fingerprint(effects)
        if fp == self._last_fingerprint:
            return
        msg = await sync_weekend_luck_boost(
            self.bot.async_session_factory,
            enabled=settings.weekend_luck_enabled,
            luck_percent=settings.weekend_luck_percent,
            timezone=settings.weekend_luck_timezone,
        )
        self._last_fingerprint = fp
        if msg:
            _LOG.info(msg)

    @tasks.loop(minutes=1)
    async def _game_events_loop(self) -> None:
        try:
            await self._sync_once()
        except SQLAlchemyError:
            _LOG.exception("game events sync failed")

    @_game_events_loop.before_loop
    async def _game_events_before(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self._sync_once()
        except SQLAlchemyError:
            _LOG.exception("game events startup sync failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GameEventsCog(bot))
