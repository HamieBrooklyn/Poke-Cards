"""Daily and weekly crystal missions."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.chat_commands import pp_alias
from poke_pon_bot.services.crystals import CrystalsService, format_crystals
from poke_pon_bot.services.missions import (
    MissionService,
    daily_period_key,
    weekly_period_key,
)

_LOG = logging.getLogger(__name__)


def _missions_embed(
    missions: list,
    *,
    svc: MissionService,
    crystal_balance: int,
) -> discord.Embed:
    day = daily_period_key()
    week = weekly_period_key()
    daily = [m for m in missions if m.period_type == "daily"]
    weekly = [m for m in missions if m.period_type == "weekly"]

    e = discord.Embed(
        title="Missions",
        description=(
            "Earn **Crystals** 💎 from daily and weekly goals. "
            "Card missions count only when you **claim from `/cd` drops** "
            "(not trades or auctions).\n"
            f"**Your balance:** {format_crystals(crystal_balance)}"
        ),
    )
    if daily:
        lines = []
        for m in sorted(daily, key=lambda x: x.slot):
            lines.append(
                f"**{m.slot + 1}.** {svc.describe_mission(m)}\n"
                f"   → {format_crystals(m.reward_crystals)} · {svc.status_line(m)}"
            )
        e.add_field(
            name=f"Daily (resets UTC midnight · {day})",
            value="\n".join(lines),
            inline=False,
        )
    if weekly:
        m = weekly[0]
        e.add_field(
            name=f"Weekly (resets Monday UTC · {week})",
            value=(
                f"{svc.describe_mission(m)}\n"
                f"→ {format_crystals(m.reward_crystals)} · {svc.status_line(m)}"
            ),
            inline=False,
        )
    if not daily and not weekly:
        e.description = "Could not load missions. Try again."
    e.set_footer(text="Complete a mission, then press Claim below.")
    return e


class ClaimMissionButton(discord.ui.Button):
    def __init__(self, *, mission_id: int, label: str, row: int) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.success,
            custom_id=f"mission_claim:{mission_id}",
            row=row,
        )
        self._mission_id = mission_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view: MissionsView = self.view  # type: ignore[assignment]
        await view.handle_claim(interaction, self._mission_id)


class MissionsView(discord.ui.View):
    def __init__(
        self,
        cog: "MissionsCog",
        *,
        owner_id: int,
        missions: list,
    ) -> None:
        super().__init__(timeout=300.0)
        self._cog = cog
        self._owner_id = owner_id
        self._missions = missions
        svc = MissionService()
        claimable = [
            m
            for m in missions
            if m.claimed_at is None and m.progress >= m.target
        ]
        for i, m in enumerate(claimable[:4]):
            short = m.period_type[:1].upper() + str(m.slot + 1)
            self.add_item(
                ClaimMissionButton(
                    mission_id=m.id,
                    label=f"Claim {short} ({m.reward_crystals}💎)",
                    row=i // 2,
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "These are not your missions.",
                ephemeral=True,
            )
            return False
        return True

    async def handle_claim(self, interaction: discord.Interaction, mission_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        crystals = CrystalsService()
        svc = MissionService()
        try:
            async with self._cog.bot.async_session_factory() as session:
                mission, err = await svc.claim_mission(
                    session,
                    crystals,
                    discord_user_id=self._owner_id,
                    mission_id=mission_id,
                )
                if err:
                    await interaction.followup.send(err, ephemeral=True)
                    return
                balance = await crystals.get_balance(session, self._owner_id)
                refreshed = await svc.list_active_missions(session, self._owner_id)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("mission claim user=%s id=%s", self._owner_id, mission_id)
            await interaction.followup.send(
                "Could not claim — try again.",
                ephemeral=True,
            )
            return

        assert mission is not None
        embed = _missions_embed(
            refreshed,
            svc=svc,
            crystal_balance=balance,
        )
        new_view = MissionsView(self._cog, owner_id=self._owner_id, missions=refreshed)
        if interaction.message is not None:
            try:
                await interaction.message.edit(embed=embed, view=new_view)
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            f"Claimed **{format_crystals(mission.reward_crystals)}**! "
            f"Balance: **{format_crystals(balance)}**.",
            ephemeral=True,
        )


class MissionsCog(commands.Cog):
    """Mission board and crystal rewards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._missions = MissionService()
        self._crystals = CrystalsService()

    @commands.hybrid_command(
        name="missions",
        aliases=[pp_alias("missions")],
        description="Daily and weekly missions for Crystals — chat: ppmissions",
    )
    async def missions_cmd(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        uid = ctx.author.id
        try:
            async with self.bot.async_session_factory() as session:
                rows = await self._missions.list_active_missions(session, uid)
                balance = await self._crystals.get_balance(session, uid)
        except SQLAlchemyError:
            _LOG.exception("missions list user=%s", uid)
            await ctx.send("Could not load missions. Try again.", ephemeral=False)
            return

        embed = _missions_embed(rows, svc=self._missions, crystal_balance=balance)
        view = MissionsView(self, owner_id=uid, missions=rows)
        await ctx.send(embed=embed, view=view, ephemeral=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MissionsCog(bot))
