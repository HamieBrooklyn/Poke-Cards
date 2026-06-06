"""Guild / server milestones — stats display and admin configuration."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.guild_milestones import (
    get_milestone_status,
    get_settings,
    milestone_embed,
    serialize_milestone_status,
    serialize_settings,
    sync_top_member_roles,
    upsert_settings,
)
_LOG = logging.getLogger(__name__)


def _guild_member_ids(guild: discord.Guild | None) -> set[int] | None:
    if guild is None:
        return None
    ids = {m.id for m in guild.members if not m.bot}
    return ids or None


class GuildMilestonesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_group(
        name="servermilestones",
        aliases=["guildmilestones", "serverstats"],
        description="Server pack milestones and configuration",
    )
    async def servermilestones_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        if ctx.guild is None:
            await ctx.send("Use this command inside a server.", ephemeral=True)
            return
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        try:
            async with self.bot.async_session_factory() as session:
                status = await get_milestone_status(session, ctx.guild.id)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("servermilestones status guild=%s", ctx.guild.id)
            await ctx.send("Could not load server milestones.", ephemeral=False)
            return
        embed = milestone_embed(ctx.guild.name, status)
        await ctx.send(embed=embed, ephemeral=False)

    @servermilestones_group.command(
        name="config",
        description="Set milestone announcement channel and optional top-member roles (Manage Server)",
    )
    @app_commands.describe(
        channel="Channel for pack milestone announcements",
        trader_role="Role granted to #1 trader (completed trades in this server)",
        collector_role="Role granted to #1 collector (unique cards owned)",
        auto_roles="Enable automatic top-role assignment",
    )
    async def servermilestones_config(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        trader_role: discord.Role | None = None,
        collector_role: discord.Role | None = None,
        auto_roles: bool | None = None,
    ) -> None:
        if ctx.guild is None:
            await ctx.send("Use this command inside a server.", ephemeral=True)
            return
        perms = ctx.author.guild_permissions if isinstance(ctx.author, discord.Member) else None
        if perms is None or not perms.manage_guild:
            await ctx.send("You need **Manage Server** to configure milestones.", ephemeral=True)
            return
        if ctx.interaction:
            await ctx.defer(ephemeral=True)

        try:
            async with self.bot.async_session_factory() as session:
                row = await upsert_settings(
                    session,
                    guild_id=ctx.guild.id,
                    announcement_channel_id=channel.id if channel else None,
                    top_trader_role_id=trader_role.id if trader_role else None,
                    top_collector_role_id=collector_role.id if collector_role else None,
                    auto_roles_enabled=auto_roles,
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("servermilestones config guild=%s", ctx.guild.id)
            await ctx.send("Could not save settings.", ephemeral=True)
            return

        parts = ["**Server milestone settings saved.**"]
        if channel:
            parts.append(f"Announcements: {channel.mention}")
        if trader_role:
            parts.append(f"Top trader role: {trader_role.mention}")
        if collector_role:
            parts.append(f"Top collector role: {collector_role.mention}")
        if auto_roles is not None:
            parts.append(f"Auto roles: **{'on' if auto_roles else 'off'}**")
        await ctx.send("\n".join(parts), ephemeral=True)

        if auto_roles and ctx.guild is not None:
            member_ids = _guild_member_ids(ctx.guild)
            if member_ids:
                asyncio.create_task(
                    sync_top_member_roles(self.bot, ctx.guild, settings=row, member_ids=member_ids)
                )

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GuildMilestonesCog(bot))
