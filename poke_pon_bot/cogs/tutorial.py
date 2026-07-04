"""Slash/chat ``/tutorial`` — start or resume the one-time DM onboarding."""

from __future__ import annotations

import discord
from discord.ext import commands

from poke_pon_bot.chat_commands import pp_chat_aliases
from poke_pon_bot.services.tutorial import (
    is_tutorial_complete,
    send_current_step_dm,
    start_tutorial,
    tutorial_enabled,
)


class TutorialCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="tutorial",
        aliases=[*pp_chat_aliases("tutorial", "tut")],
        description="Start or resume the 5-step PokePon quest chain in your DMs (Crystals + Member role).",
    )
    async def tutorial_cmd(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        settings = self.bot.settings
        if not tutorial_enabled(settings):
            await ctx.send(
                "The tutorial is not configured on this bot instance.",
                ephemeral=True,
            )
            return
        uid = ctx.author.id
        if await is_tutorial_complete(self.bot.async_session_factory, uid):
            await ctx.send(
                "You have already completed the tutorial. Thanks!",
                ephemeral=True,
            )
            return
        guild_id = ctx.guild.id if ctx.guild is not None else settings.tutorial_guild_id
        await start_tutorial(
            self.bot.async_session_factory,
            discord_user_id=uid,
            guild_id=guild_id,
        )
        await send_current_step_dm(self.bot, uid)
        await ctx.send(
            "Check your **DMs** — use the **`/`** commands shown in each step. "
            "If nothing arrives, enable DMs from server members and try again.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    if tutorial_enabled(bot.settings):
        await bot.add_cog(TutorialCog(bot))
