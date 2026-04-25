"""Bot client: intents, extension loading, slash command sync."""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord.ext import commands

_LOG = logging.getLogger(__name__)

from poke_pon_bot.config import Settings, load_settings
from poke_pon_bot.db.session import async_session_factory, create_engine_from_url


class PokePonBot(commands.Bot):
    """Application bot — add cogs in setup_hook and keep startup logic here."""

    def __init__(self, *, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.dev_guild_id = settings.dev_guild_id

        Path("data").mkdir(parents=True, exist_ok=True)
        self.engine = create_engine_from_url(settings.database_url)
        self.async_session_factory = async_session_factory(self.engine)

    async def setup_hook(self) -> None:
        await self.load_extension("poke_pon_bot.cogs.general")
        await self.load_extension("poke_pon_bot.cogs.gacha")

        if self.dev_guild_id is not None:
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            _LOG.info(
                "Slash commands synced to guild %s (%s commands).",
                self.dev_guild_id,
                len(synced),
            )
        else:
            synced = await self.tree.sync()
            _LOG.info("Slash commands synced globally (%s commands).", len(synced))

    async def close(self) -> None:
        await super().close()
        await self.engine.dispose()


def run_bot() -> None:
    settings = load_settings(require_discord_token=True)
    bot = PokePonBot(settings=settings)
    assert settings.discord_token is not None
    bot.run(settings.discord_token)
