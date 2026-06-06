"""Discord ``/referral`` — personal invite link and referral progress."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from poke_pon_bot.models.guild_referral import GuildReferral
from poke_pon_bot.web.user_profiles import resolve_user_profiles
from poke_pon_bot.services.personal_invites import get_or_create_personal_invite
from poke_pon_bot.services.referrals import (
    REFERRAL_CD_USES_REQUIRED,
    REFERRAL_CRYSTAL_REWARD,
    REFERRAL_FIRST_PACK_INVITEE_CRYSTALS,
    REFERRAL_FIRST_PACK_INVITER_CRYSTALS,
    REFERRAL_MAX_REWARDS_PER_INVITER,
    build_invitee_referral_status,
    build_referral_dashboard,
    count_inviter_rewards,
)

_LOG = logging.getLogger(__name__)


def _progress_bar(uses: int, required: int, width: int = 12) -> str:
    filled = min(width, int(width * uses / required)) if required > 0 else 0
    return "█" * filled + "░" * (width - filled)


class ReferralsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="referral",
        description="Your invite link, invited friends, and referral progress",
    )
    async def referral(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        bot_settings = self.bot.settings
        guild_id = bot_settings.referral_invite_guild_id
        channel_id = bot_settings.referral_invite_channel_id

        async with self.bot.async_session_factory() as session:
            invite_url: str | None = None
            if guild_id is not None:
                invite = await get_or_create_personal_invite(
                    self.bot,
                    session,
                    discord_user_id=uid,
                    guild_id=int(guild_id),
                    preferred_channel_id=channel_id,
                )
                if invite is not None:
                    invite_url = invite.url

            dashboard = await build_referral_dashboard(
                session, uid, invitee_profiles={}
            )
            rewards_earned = await count_inviter_rewards(session, uid)

            inviter_ids: list[int] = []
            invitee_row = await session.get(GuildReferral, uid)
            if invitee_row is not None:
                inviter_ids.append(int(invitee_row.inviter_discord_id))
            invitee_status = None
            if invitee_row is not None:
                profiles = await resolve_user_profiles(
                    session, self.bot, inviter_ids
                )
                invitee_status = await build_invitee_referral_status(
                    session, uid, inviter_profiles=profiles
                )
            await session.commit()

        summary = dashboard["summary"]
        embed = discord.Embed(
            title="Referrals",
            color=discord.Color.blurple(),
            description=(
                f"**First pack:** you and a new friend each earn Crystals on their first **`/cd`** "
                f"({REFERRAL_FIRST_PACK_INVITEE_CRYSTALS} 💎 them, "
                f"{REFERRAL_FIRST_PACK_INVITER_CRYSTALS} 💎 you). "
                f"**Milestone:** **{REFERRAL_CRYSTAL_REWARD}** 💎 for you when they reach "
                f"**{REFERRAL_CD_USES_REQUIRED}** packs (up to **{REFERRAL_MAX_REWARDS_PER_INVITER}** friends)."
            ),
        )

        if invite_url:
            embed.add_field(
                name="Your invite link",
                value=f"[Copy & share]({invite_url})\n`{invite_url}`",
                inline=False,
            )
        else:
            embed.add_field(
                name="Your invite link",
                value="Not available right now — check **Profile → Referrals** on the website.",
                inline=False,
            )

        embed.add_field(
            name="Your invites",
            value=(
                f"**{summary['total_invited']}** friends tracked · "
                f"**{summary['in_progress']}** in progress · "
                f"**{rewards_earned}** / **{REFERRAL_MAX_REWARDS_PER_INVITER}** "
                f"milestone rewards earned"
            ),
            inline=False,
        )

        referrals = dashboard["referrals"][:5]
        if referrals:
            lines: list[str] = []
            for row in referrals:
                uses = int(row["cd_uses"])
                req = int(row["cd_uses_required"])
                name = row["display_name"]
                bar = _progress_bar(uses, req)
                extra = ""
                if row.get("first_pack_rewarded"):
                    extra = " · first pack ✓"
                lines.append(f"**{name}** — `{bar}` {uses}/{req}{extra}")
            embed.add_field(
                name="Recent friends",
                value="\n".join(lines),
                inline=False,
            )

        if invitee_status is not None:
            uses = int(invitee_status["cd_uses"])
            req = int(invitee_status["cd_uses_required"])
            inv_name = invitee_status["inviter_display_name"]
            bar = _progress_bar(uses, req)
            first = (
                f" · you earned **{invitee_status['invitee_crystals_awarded']}** 💎 on pack 1"
                if invitee_status.get("first_pack_rewarded")
                else f" · **{REFERRAL_FIRST_PACK_INVITEE_CRYSTALS}** 💎 on your first pack"
            )
            embed.add_field(
                name="You were invited",
                value=(
                    f"Invited by **{inv_name}**\n"
                    f"`{bar}` **{uses}** / **{req}** card drops{first}"
                ),
                inline=False,
            )

        embed.set_footer(
            text="Profile → Referrals on pokepon.org for the full list and copy button."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReferralsCog(bot))
