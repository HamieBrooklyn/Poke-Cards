"""Tutorial: DM new members, track commands, guild welcome when the bot joins."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from poke_pon_bot.services.tutorial import (
    grant_member_role,
    handle_command_for_tutorial,
    is_main_tutorial_guild,
    is_tutorial_complete,
    send_current_step_dm,
    start_tutorial,
    tutorial_enabled,
)
from poke_pon_bot.services.tutorial_welcome import ensure_verify_panel, register_verify_view

_LOG = logging.getLogger(__name__)

_GUILD_GREET = (
    "Thanks for adding **PokePon**!\n\n"
    "New members complete verification with the **Verify** button in the welcome channel "
    "(or **`/tutorial`** in DMs). That grants the **Member** role.\n\n"
    "Use **`/help`** for commands."
)


def setup_tutorial_listener(bot: commands.Bot) -> None:
    if getattr(bot, "_pokepon_tutorial_listener", False):
        return
    if not tutorial_enabled(bot.settings):
        _LOG.info("Tutorial listener disabled (set TUTORIAL_GUILD_ID to enable).")
        return

    register_verify_view(bot)

    async def _tutorial_on_ready() -> None:
        await ensure_verify_panel(bot)

    async def _tutorial_on_guild_join(guild: discord.Guild) -> None:
        if guild.me is None:
            return
        channel = guild.system_channel
        if channel is None:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channel = ch
                    break
        if channel is None:
            return
        try:
            await channel.send(_GUILD_GREET)
        except discord.Forbidden:
            _LOG.info("Could not post tutorial greet in guild %s", guild.id)
        if is_main_tutorial_guild(guild.id, bot.settings):
            await ensure_verify_panel(bot)

    async def _tutorial_on_member_join(member: discord.Member) -> None:
        if member.bot:
            return
        settings = bot.settings
        if not is_main_tutorial_guild(member.guild.id, settings):
            return
        if await is_tutorial_complete(bot.async_session_factory, member.id):
            await grant_member_role(
                bot,
                settings,
                discord_user_id=member.id,
                guild_id=member.guild.id,
            )
            return
        try:
            await start_tutorial(
                bot.async_session_factory,
                discord_user_id=member.id,
                guild_id=member.guild.id,
            )
            await send_current_step_dm(bot, member.id)
        except Exception:
            _LOG.exception("Tutorial start failed for member %s", member.id)

    bot.add_listener(_tutorial_on_ready, "on_ready")
    bot.add_listener(_tutorial_on_guild_join, "on_guild_join")
    bot.add_listener(_tutorial_on_member_join, "on_member_join")

    async def _tutorial_on_command_completion(ctx: commands.Context) -> None:
        if ctx.interaction is not None:
            return
        try:
            await handle_command_for_tutorial(bot, ctx)
        except Exception:
            _LOG.exception("Tutorial command hook failed for user %s", ctx.author.id)

    async def _tutorial_on_app_command_completion(
        interaction: discord.Interaction,
        command: app_commands.Command,
    ) -> None:
        if interaction.user is None or interaction.user.bot:
            return
        # Hybrid slash commands populate kwargs on the invocation baton, not on a fresh
        # Context.from_interaction() (which always has kwargs={}).
        ctx = getattr(interaction, "_baton", None)
        if ctx is None:
            ctx = await bot.get_context(interaction)
        if getattr(ctx, "command", None) is None:
            wrapped = getattr(command, "wrapped", None)
            ctx.command = wrapped if wrapped is not None else command
        try:
            await handle_command_for_tutorial(bot, ctx)
        except Exception:
            _LOG.exception("Tutorial slash hook failed for user %s", interaction.user.id)

    bot.add_listener(_tutorial_on_command_completion, "on_command_completion")
    bot.add_listener(_tutorial_on_app_command_completion, "on_app_command_completion")

    bot._pokepon_tutorial_listener = True  # type: ignore[attr-defined]
    _LOG.info(
        "Tutorial listener enabled for guild %s (Member role %s).",
        bot.settings.tutorial_guild_id,
        bot.settings.tutorial_member_role_id,
    )
