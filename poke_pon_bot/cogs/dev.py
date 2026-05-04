"""Developer-only utilities (Pokedollars, etc.); restricted by ``DEVELOPER_IDS`` in the environment."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.config import Settings
from poke_pon_bot.services.wallet import CURRENCY_NAME, WalletService, format_pokedollars

_LOG = logging.getLogger(__name__)

_MAX_POKEDOLLARS = 2_147_483_647


class DevCog(commands.Cog):
    """Commands gated to user IDs in ``Settings.developer_ids``."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        s: object = getattr(bot, "settings", None)
        self._dev_ids: frozenset[int] = (
            s.developer_ids if isinstance(s, Settings) else frozenset()
        )
        self._wallet = WalletService()

    def _is_dev(self, user_id: int) -> bool:
        return user_id in self._dev_ids

    dev = app_commands.Group(
        name="dev",
        description="Bot developer tools (set DEVELOPER_IDS in the environment).",
    )

    @dev.command(
        name="pokedollars",
        description=f"Set a user’s {CURRENCY_NAME} balance (developers only).",
    )
    @app_commands.describe(
        amount="New balance (this replaces the current balance).",
        user="Whose balance to set; omit to set your own.",
    )
    async def dev_pokedollars(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 0, _MAX_POKEDOLLARS],
        user: discord.User | None = None,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set in the bot’s environment.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.",
                ephemeral=True,
            )
            return
        target = user or interaction.user
        try:
            async with self.bot.async_session_factory() as session:
                new_bal = await self._wallet.set_balance(session, target.id, int(amount))
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("dev set pokedollars for user %s", target.id)
            await interaction.response.send_message(
                "Could not save the balance. Try again.",
                ephemeral=True,
            )
            return
        if user is not None:
            line = f"Set **{target}**'s {CURRENCY_NAME} to **{format_pokedollars(new_bal)}**."
        else:
            line = f"Set your {CURRENCY_NAME} to **{format_pokedollars(new_bal)}**."
        await interaction.response.send_message(line, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DevCog(bot))
