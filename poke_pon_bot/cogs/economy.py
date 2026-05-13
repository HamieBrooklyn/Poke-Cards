"""Pokedollars daily claim and balance (spending TBD)."""

from __future__ import annotations

import logging
import math

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.crystals import CrystalsService, format_crystals
from poke_pon_bot.services.topgg_vote import (
    TopggAuthError,
    TopggRateLimitError,
    fetch_active_discord_vote,
)
from poke_pon_bot.services.wallet import (
    AlreadyClaimedVoteRewardError,
    AlreadyClaimedTodayError,
    CURRENCY_NAME,
    DAILY_CLAIM_MAX,
    DAILY_CLAIM_MIN,
    VOTE_CLAIM_MAX,
    VOTE_CLAIM_MIN,
    WalletService,
    format_pokedollars,
)

_LOG = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total = int(math.ceil(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class EconomyCog(commands.Cog):
    """Wallet commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wallet = WalletService()
        self._crystals = CrystalsService()

    @commands.hybrid_command(
        name="daily",
        aliases=["pcdaily"],
        description=(
            f"Claim your daily {CURRENCY_NAME} ({DAILY_CLAIM_MIN}–{DAILY_CLAIM_MAX} ₽, once per UTC day)."
        ),
    )
    async def daily(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        uid = ctx.author.id
        try:
            async with self.bot.async_session_factory() as session:
                try:
                    result = await self._wallet.try_daily_claim(session, uid)
                    await session.commit()
                except AlreadyClaimedTodayError:
                    await session.rollback()
                    await ctx.send(
                        "You already claimed your daily reward today (UTC). "
                        "Come back after midnight UTC for more "
                        f"{CURRENCY_NAME}.",
                        ephemeral=False,
                    )
                    return
                except SQLAlchemyError:
                    await session.rollback()
                    _LOG.exception("daily claim DB error for user %s", uid)
                    await ctx.send(
                        "Something went wrong saving your claim. Try again in a moment.",
                        ephemeral=False,
                    )
                    return
        except SQLAlchemyError:
            _LOG.exception("daily claim session error for user %s", uid)
            await ctx.send(
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
        await ctx.send(embed=embed, ephemeral=False)

    @commands.hybrid_command(
        name="balance",
        aliases=["pcbal"],
        description=f"Check {CURRENCY_NAME} balance (yours or another user's).",
    )
    @app_commands.describe(
        user="Whose balance to show — leave empty for yours",
    )
    async def balance(self, ctx: commands.Context, user: discord.User | None = None) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        target = user or ctx.author
        if target.bot:
            await ctx.send(
                "Bots don't have a Pokedollars balance here.",
                ephemeral=False,
            )
            return
        uid = target.id
        try:
            async with self.bot.async_session_factory() as session:
                bal = await self._wallet.get_balance(session, uid)
                crystal_bal = await self._crystals.get_balance(session, uid)
        except SQLAlchemyError:
            _LOG.exception("balance lookup for user %s", uid)
            await ctx.send(
                "Could not load that balance. Try again later.",
                ephemeral=False,
            )
            return
        line = f"**{CURRENCY_NAME}:** {format_pokedollars(bal)} · {format_crystals(crystal_bal)}"
        if user is None:
            await ctx.send(line, ephemeral=False)
        else:
            await ctx.send(
                f"{target.mention} · {line}",
                ephemeral=False,
            )

    @commands.hybrid_command(
        name="vote",
        aliases=["pvote", "pcvote"],
        description=(
            f"Top.gg vote reward ({VOTE_CLAIM_MIN}–{VOTE_CLAIM_MAX} ₽). "
            f"Claim here or via webhook if enabled."
        ),
    )
    async def poke_vote(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        uid = ctx.author.id
        settings = self.bot.settings
        vote_url = settings.topgg_vote_url

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Vote on Top.gg", style=discord.ButtonStyle.link, url=vote_url))

        if not settings.topgg_api_token:
            await ctx.send(
                "Vote rewards aren't configured yet. You can still vote with the button below.",
                view=view,
                ephemeral=False,
            )
            return

        try:
            status = await fetch_active_discord_vote(settings.topgg_api_token, uid)
        except TopggAuthError:
            await ctx.send("Vote check failed (server config).", ephemeral=False)
            return
        except TopggRateLimitError:
            await ctx.send("Top.gg rate-limited. Try again in a minute.", ephemeral=False)
            return
        except httpx.HTTPError:
            _LOG.exception("Top.gg vote HTTP error for user %s", uid)
            await ctx.send("Couldn't reach Top.gg. Try again shortly.", ephemeral=False)
            return

        if status is None:
            await ctx.send(
                "No active vote found yet. Vote on Top.gg, then run **`/vote`** again.",
                view=view,
                ephemeral=False,
            )
            return
        try:
            async with self.bot.async_session_factory() as session:
                try:
                    result = await self._wallet.try_vote_claim(
                        session,
                        uid,
                        vote_created_at=status.created_at,
                        vote_expires_at=status.expires_at,
                    )
                    await session.commit()
                except AlreadyClaimedVoteRewardError as exc:
                    await session.rollback()
                    await ctx.send(
                        f"Already claimed. Vote again in **{_format_duration(exc.retry_after_seconds)}**.",
                        view=view,
                        ephemeral=False,
                    )
                    return
                except SQLAlchemyError:
                    await session.rollback()
                    _LOG.exception("vote claim DB error for user %s", uid)
                    await ctx.send("Something went wrong. Try again shortly.", ephemeral=False)
                    return
        except SQLAlchemyError:
            _LOG.exception("vote claim session error for user %s", uid)
            await ctx.send("Something went wrong. Try again shortly.", ephemeral=False)
            return

        crystal_suffix = (
            f" · +**{result.crystals_credited}** 💎" if result.crystals_credited else ""
        )
        await ctx.send(
            f"+**{format_pokedollars(result.amount)}** ({CURRENCY_NAME}){crystal_suffix} · "
            f"balance **{format_pokedollars(result.new_balance)}**",
            view=view,
            ephemeral=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomyCog(bot))
