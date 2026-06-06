"""Seasonal set chase — status, rewards, env sync."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.set_chase import (
    build_status,
    claim_personal_reward,
    ensure_season_from_settings,
    format_set_chase_embed,
    reconcile_community_payout,
)

_LOG = logging.getLogger(__name__)


class SetChaseCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await self._sync_configured_season()

    async def _sync_configured_season(self) -> None:
        settings = self.bot.settings
        if not settings.set_chase_enabled:
            return
        try:
            async with self.bot.async_session_factory() as session:
                row = await ensure_season_from_settings(
                    session,
                    enabled=settings.set_chase_enabled,
                    set_code=settings.set_chase_set_code,
                    title=settings.set_chase_title,
                    starts_at=settings.set_chase_starts_at,
                    ends_at=settings.set_chase_ends_at,
                    global_target=settings.set_chase_global_target,
                    drop_boost_percent=settings.set_chase_drop_boost_percent,
                    completion_threshold_pct=settings.set_chase_completion_threshold_pct,
                    reward_crystals=settings.set_chase_reward_crystals,
                    reward_pokedollars=settings.set_chase_reward_pokedollars,
                    community_participation_crystals=settings.set_chase_community_reward_crystals,
                )
                paid = await reconcile_community_payout(session)
                await session.commit()
                if row is not None:
                    _LOG.info(
                        "Set chase season ready: %s (%s → %s)",
                        row.season_key,
                        row.starts_at.isoformat(),
                        row.ends_at.isoformat(),
                    )
                if paid:
                    _LOG.info("Set chase community payout reconciled for %s participant(s)", paid)
        except SQLAlchemyError:
            _LOG.exception("Set chase season sync failed")

    @app_commands.command(
        name="setchase",
        description="View the seasonal set chase — community progress and your binder reward.",
    )
    @app_commands.describe(action="Claim your personal reward when you reach the completion goal.")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="View progress", value="view"),
            app_commands.Choice(name="Claim reward", value="claim"),
        ]
    )
    async def setchase(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str] | None = None,
    ) -> None:
        uid = interaction.user.id
        mode = action.value if action is not None else "view"
        try:
            async with self.bot.async_session_factory() as session:
                if mode == "claim":
                    season, err = await claim_personal_reward(session, discord_user_id=uid)
                    if err:
                        await interaction.response.send_message(err, ephemeral=True)
                        return
                    await session.commit()
                    status = await build_status(session, discord_user_id=uid)
                    if status is None:
                        await interaction.response.send_message(
                            "Reward claimed, but the season ended.",
                            ephemeral=True,
                        )
                        return
                    msg = (
                        f"🎁 **Set chase reward claimed!**\n"
                        f"+**{int(season.reward_crystals)}** 💎 · "
                        f"+**₽{int(season.reward_pokedollars):,}**\n\n"
                        f"{format_set_chase_embed(status)}"
                    )
                    await interaction.response.send_message(msg, ephemeral=True)
                    return

                status = await build_status(session, discord_user_id=uid)
                if status is None:
                    await interaction.response.send_message(
                        "There is no active **set chase** right now. Check back later!",
                        ephemeral=True,
                    )
                    return
                await interaction.response.send_message(
                    format_set_chase_embed(status),
                    ephemeral=True,
                )
        except SQLAlchemyError:
            _LOG.exception("setchase command failed user=%s", uid)
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong loading set chase progress.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong loading set chase progress.",
                    ephemeral=True,
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetChaseCog(bot))
