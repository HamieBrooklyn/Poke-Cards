"""Bot client: intents, extension loading, slash command sync."""

from __future__ import annotations

import discord
from discord.ext import commands

from poke_pon_bot.config import load_settings


class PokePonBot(commands.Bot):
    """Application bot — add cogs in setup_hook and keep startup logic here."""

    def __init__(self, *, dev_guild_id: int | None) -> None:
        intents = discord.Intents.default()
        # Prefix commands need the Message Content privileged intent in the Developer Portal.
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )
        self.dev_guild_id = dev_guild_id

    async def setup_hook(self) -> None:
        await self.load_extension("poke_pon_bot.cogs.general")

        if self.dev_guild_id is not None:
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


def run_bot() -> None:
    settings = load_settings()
    bot = PokePonBot(dev_guild_id=settings.dev_guild_id)
    bot.run(settings.discord_token)
