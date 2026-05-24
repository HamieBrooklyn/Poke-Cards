"""Auto-claim Top.gg vote rewards after successful bot commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from poke_pon_bot.services.topgg_auto_claim import try_auto_claim_after_command

_LOG = logging.getLogger(__name__)


async def _auto_claim_task(
    bot: commands.Bot,
    user: discord.abc.User,
    *,
    command_name: str | None,
    cog: object | None,
) -> None:
    try:
        await try_auto_claim_after_command(
            bot,
            user,
            command_name=command_name,
            cog=cog,
        )
    except Exception:
        _LOG.exception("Top.gg vote auto-claim failed for user %s", user.id)


def setup_vote_claim_listener(bot: commands.Bot) -> None:
    if getattr(bot, "_pokepon_vote_claim_listener", False):
        return
    if not bot.settings.topgg_api_token or not bot.settings.topgg_auto_claim_enabled:
        return

    async def _vote_on_command_completion(ctx: commands.Context) -> None:
        if ctx.interaction is not None:
            return
        if ctx.author.bot or not ctx.command:
            return
        bot.loop.create_task(
            _auto_claim_task(
                bot,
                ctx.author,
                command_name=ctx.command.name,
                cog=ctx.cog,
            )
        )

    async def _vote_on_app_command_completion(
        interaction: discord.Interaction,
        command: app_commands.Command,
    ) -> None:
        if interaction.user is None or interaction.user.bot:
            return
        cog = command.binding if isinstance(command.binding, commands.Cog) else None
        bot.loop.create_task(
            _auto_claim_task(
                bot,
                interaction.user,
                command_name=command.name,
                cog=cog,
            )
        )

    bot.add_listener(_vote_on_command_completion, "on_command_completion")
    bot.add_listener(_vote_on_app_command_completion, "on_app_command_completion")

    bot._pokepon_vote_claim_listener = True  # type: ignore[attr-defined]
    _LOG.info(
        "Top.gg vote auto-claim enabled (poll every %ss after commands).",
        bot.settings.topgg_vote_poll_min_seconds,
    )
