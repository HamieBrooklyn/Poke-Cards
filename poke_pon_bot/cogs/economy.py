"""Pokedollars daily claim and balance (spending TBD)."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.wallet import (
    AlreadyClaimedTodayError,
    CURRENCY_NAME,
    DAILY_CLAIM_MAX,
    DAILY_CLAIM_MIN,
    WalletService,
    format_pokedollars,
)

_LOG = logging.getLogger(__name__)


class EconomyCog(commands.Cog):
    """Wallet commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wallet = WalletService()

    @app_commands.command(
        name="daily",
        description=f"Claim your daily {CURRENCY_NAME} ({DAILY_CLAIM_MIN}–{DAILY_CLAIM_MAX} ₽, once per UTC day).",
    )
    async def daily(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        uid = interaction.user.id
        try:
            async with self.bot.async_session_factory() as session:
                try:
                    result = await self._wallet.try_daily_claim(session, uid)
                    await session.commit()
                except AlreadyClaimedTodayError:
                    await session.rollback()
                    await interaction.followup.send(
                        "You already claimed your daily reward today (UTC). "
                        "Come back after midnight UTC for more "
                        f"{CURRENCY_NAME}.",
                        ephemeral=False,
                    )
                    return
                except SQLAlchemyError:
                    await session.rollback()
                    _LOG.exception("daily claim DB error for user %s", uid)
                    await interaction.followup.send(
                        "Something went wrong saving your claim. Try again in a moment.",
                        ephemeral=False,
                    )
                    return
        except SQLAlchemyError:
            _LOG.exception("daily claim session error for user %s", uid)
            await interaction.followup.send(
                "Something went wrong. Try again in a moment.",
                ephemeral=False,
            )
            return

        embed = discord.Embed(
            title="Daily reward",
            description=(
                f"You received **{format_pokedollars(result.amount)}** "
                f"({CURRENCY_NAME}).\n"
                f"**Balance:** {format_pokedollars(result.new_balance)}"
            ),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="balance",
        description=f"Check your {CURRENCY_NAME} balance.",
    )
    async def balance(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        uid = interaction.user.id
        try:
            async with self.bot.async_session_factory() as session:
                bal = await self._wallet.get_balance(session, uid)
        except SQLAlchemyError:
            _LOG.exception("balance lookup for user %s", uid)
            await interaction.followup.send(
                "Could not load your balance. Try again later.",
                ephemeral=False,
            )
            return
        await interaction.followup.send(
            f"**{CURRENCY_NAME}:** {format_pokedollars(bal)}",
            ephemeral=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomyCog(bot))
