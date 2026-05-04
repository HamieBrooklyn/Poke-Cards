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
        if settings.discord_message_content_intent:
            # Must match Developer Portal: **Bot** tab → **Privileged Gateway Intents** →
            # **Message Content Intent** (not OAuth2 URL Generator scopes, not the invite permissions grid).
            intents.message_content = True
        super().__init__(
            # `c` + `d` = `cd` (card drop), `c`+`s` = `cs`, `c`+`v` = `cv`, `c`+`help` = `chelp` (Gachapon-style)
            command_prefix=commands.when_mentioned_or("c"),
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.dev_guild_id = settings.dev_guild_id

        Path("data").mkdir(parents=True, exist_ok=True)
        self.engine = create_engine_from_url(settings.database_url)
        self.async_session_factory = async_session_factory(self.engine)
        if not settings.discord_message_content_intent:
            _LOG.info(
                "Chat commands cd / cs / cv (prefix c) are off. Set DISCORD_MESSAGE_CONTENT_INTENT=1 in .env and enable "
                "Message Content on the applications → Bot → Privileged Gateway Intents page, then restart."
            )

    async def setup_hook(self) -> None:
        await self.load_extension("poke_pon_bot.cogs.general")
        await self.load_extension("poke_pon_bot.cogs.economy")
        await self.load_extension("poke_pon_bot.cogs.dev")
        await self.load_extension("poke_pon_bot.cogs.gacha")
        await self.load_extension("poke_pon_bot.cogs.duel")
        await self.load_extension("poke_pon_bot.cogs.trade")

        if self.dev_guild_id is not None:
            # Copy globals into the dev guild, then drop globals from the in-memory tree and sync
            # an *empty* global list. Otherwise Discord keeps stale global command IDs from older
            # versions — the command picker may show duplicate /collection entries (old + new).
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            self.tree.clear_commands(guild=None)
            guild_cmds = await self.tree.sync(guild=guild)
            await self.tree.sync(guild=None)
            _LOG.info(
                "Slash: guild %s has %s commands; cleared stale global app commands for this app.",
                self.dev_guild_id,
                len(guild_cmds),
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
