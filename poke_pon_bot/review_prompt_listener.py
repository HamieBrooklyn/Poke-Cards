"""Track successful commands and send a one-time Top.gg review reminder DM."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from poke_pon_bot.services.topgg_review_prompt import (
    mark_review_acknowledged,
    should_track_command,
    try_prompt_after_command,
)

_LOG = logging.getLogger(__name__)

_ACK_PREFIX = "pokepon:topgg_review_ack:"


async def _prompt_task(bot: commands.Bot, user: discord.abc.User) -> None:
    try:
        await try_prompt_after_command(
            bot,
            session_factory=bot.async_session_factory,
            settings=bot.settings,
            user=user,
        )
    except Exception:
        _LOG.exception("Top.gg review prompt failed for user %s", user.id)


def setup_review_prompt_listener(bot: commands.Bot) -> None:
    if getattr(bot, "_pokepon_review_prompt_listener", False):
        return

    async def _review_on_command_completion(ctx: commands.Context) -> None:
        if ctx.interaction is not None:
            return
        if ctx.author.bot or not ctx.command or not should_track_command(cog=ctx.cog):
            return
        bot.loop.create_task(_prompt_task(bot, ctx.author))

    async def _review_on_app_command_completion(
        interaction: discord.Interaction,
        command: app_commands.Command,
    ) -> None:
        if interaction.user is None or interaction.user.bot:
            return
        cog = command.binding if isinstance(command.binding, commands.Cog) else None
        if not should_track_command(cog=cog):
            return
        bot.loop.create_task(_prompt_task(bot, interaction.user))

    bot.add_listener(_review_on_command_completion, "on_command_completion")
    bot.add_listener(_review_on_app_command_completion, "on_app_command_completion")

    @bot.listen("on_interaction")
    async def on_topgg_review_ack_interaction(interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id") if interaction.data else None
        if not cid or not isinstance(cid, str) or not cid.startswith(_ACK_PREFIX):
            return
        try:
            owner_id = int(cid.removeprefix(_ACK_PREFIX))
        except ValueError:
            return
        if interaction.user is None or interaction.user.id != owner_id:
            await interaction.response.send_message(
                "This reminder isn't for you.",
                ephemeral=True,
            )
            return
        await mark_review_acknowledged(bot.async_session_factory, owner_id)
        await interaction.response.edit_message(
            content="Thanks — we won't send another review reminder.",
            embed=None,
            view=None,
        )

    bot._pokepon_review_prompt_listener = True  # type: ignore[attr-defined]
    _LOG.info(
        "Top.gg review prompt listener enabled (after %s successful commands).",
        bot.settings.topgg_review_prompt_min_commands,
    )
