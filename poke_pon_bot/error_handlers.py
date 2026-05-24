"""Log command failures and reply to the user when Discord allows it."""

from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands

_LOG = logging.getLogger(__name__)

_USER_MESSAGE = (
    "Something went wrong while running that command. "
    "Details were written to the bot log — check the terminal where the bot runs."
)


def _root_cause(exc: BaseException) -> BaseException:
    if isinstance(exc, commands.CommandInvokeError) and exc.original is not None:
        return exc.original
    if isinstance(exc, app_commands.CommandInvokeError) and exc.original is not None:
        return exc.original
    return exc


def _should_skip(error: Exception) -> bool:
    if isinstance(error, commands.CommandNotFound):
        return True
    if isinstance(error, commands.UserInputError):
        return True
    if isinstance(error, app_commands.CheckFailure):
        return True
    return False


async def _send_ctx_error(ctx: commands.Context, text: str) -> None:
    """Best-effort user reply for prefix or hybrid commands."""
    try:
        if ctx.interaction is not None:
            if ctx.interaction.response.is_done():
                await ctx.send(text, ephemeral=True)
            else:
                await ctx.interaction.response.send_message(text, ephemeral=True)
            return
        if ctx.channel is not None:
            await ctx.reply(text, mention_author=False)
    except (discord.HTTPException, discord.Forbidden) as send_exc:
        _LOG.warning("Could not send command error to user: %s", send_exc)


async def _send_interaction_error(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except (discord.HTTPException, discord.Forbidden) as send_exc:
        _LOG.warning("Could not send slash error to user: %s", send_exc)


async def reply_command_failure(ctx: commands.Context) -> None:
    """Tell the user a command failed (after logging the real exception elsewhere)."""
    await _send_ctx_error(ctx, _USER_MESSAGE)


async def handle_command_error(ctx: commands.Context, error: Exception) -> None:
    if _should_skip(error):
        return
    root = _root_cause(error)
    cmd = ctx.command.qualified_name if ctx.command else "?"
    _LOG.exception("Command /%s failed for %s", cmd, ctx.author, exc_info=root)
    await _send_ctx_error(ctx, _USER_MESSAGE)


async def handle_application_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if _should_skip(error):
        return
    root = _root_cause(error)
    user = interaction.user
    cmd = interaction.command.qualified_name if interaction.command else "?"
    _LOG.exception("Slash /%s failed for %s", cmd, user, exc_info=root)
    await _send_interaction_error(interaction, _USER_MESSAGE)


def setup_error_handlers(bot: commands.Bot) -> None:
    """Attach global error listeners (idempotent)."""

    if getattr(bot, "_pokepon_error_handlers", False):
        return

    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        # Hybrid/slash invocations are reported via ``@bot.tree.error`` instead.
        if ctx.interaction is not None:
            return
        await handle_command_error(ctx, error)

    @bot.tree.error
    async def on_app_command_tree_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        await handle_application_command_error(interaction, error)

    bot._pokepon_error_handlers = True  # type: ignore[attr-defined]
