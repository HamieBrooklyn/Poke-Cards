"""Craft booster packs from item + trainer cards."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.chat_commands import pp_chat_aliases
from poke_pon_bot.cogs.gacha import _hybrid_ephemeral, _reply_card_id_below
from poke_pon_bot.services.card_roles import CRAFT_ITEM_COUNT
from poke_pon_bot.services.crafting import run_craft
from poke_pon_bot.services.instance_public_id import normalize_public_id

_LOG = logging.getLogger(__name__)


def _parse_id_list(raw: str) -> list[str]:
    parts: list[str] = []
    for chunk in (raw or "").replace("\n", ",").split(","):
        pid = normalize_public_id(chunk.strip())
        if pid:
            parts.append(pid)
    return parts


class CraftOpenPackView(discord.ui.View):
    def __init__(self, cog: CraftingCog, *, owner_id: int, pack_public_id: str) -> None:
        super().__init__(timeout=300.0)
        self._cog = cog
        self._owner_id = owner_id
        self._pack_public_id = pack_public_id

    @discord.ui.button(label="Open pack now", style=discord.ButtonStyle.success)
    async def open_pack(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user is None or interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This craft result isn't for you.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        from poke_pon_bot.cogs.packs import PacksCog

        packs_cog = self._cog.bot.get_cog("PacksCog")
        if not isinstance(packs_cog, PacksCog):
            await interaction.followup.send(
                "Pack opener is unavailable — try `/packcolv`.", ephemeral=True
            )
            return
        from poke_pon_bot.models.pack_instance import UserPackInstance
        from sqlalchemy import select

        async with self._cog.bot.async_session_factory() as session:
            pack = await session.scalar(
                select(UserPackInstance).where(
                    UserPackInstance.public_id == self._pack_public_id,
                    UserPackInstance.discord_user_id == self._owner_id,
                )
            )
        if pack is None:
            await interaction.followup.send("Pack not found.", ephemeral=True)
            return
        await packs_cog._handle_open_pack(interaction, pack.id)


class CraftingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="craft",
        aliases=[*pp_chat_aliases("craft")],
        description=(
            f"Craft a pack: {CRAFT_ITEM_COUNT} item Card IDs + 1 trainer Card ID "
            "(trainer rarity sets pack tier)."
        ),
    )
    @app_commands.describe(
        items=(
            f"Comma-separated Card IDs for {CRAFT_ITEM_COUNT} **Item** or **Energy** "
            "cards from your collection."
        ),
        trainer=(
            "Card ID for one **trainer** card (Supporter / Stadium / Tool — 3 crafts per copy)."
        ),
    )
    async def craft_cmd(
        self,
        ctx: commands.Context,
        items: str,
        trainer: str,
    ) -> None:
        await _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        item_ids = _parse_id_list(items)
        trainer_id = normalize_public_id((trainer or "").strip())
        if trainer_id is None:
            await ctx.send(
                "Invalid trainer Card ID.", ephemeral=True
            )
            return

        guild_id = ctx.guild.id if ctx.guild else None
        try:
            async with self.bot.async_session_factory() as session:
                outcome = await run_craft(
                    session,
                    discord_user_id=uid,
                    item_public_ids=item_ids,
                    trainer_public_id=trainer_id,
                    guild_id=guild_id,
                )
                if isinstance(outcome, str):
                    await session.rollback()
                    await ctx.send(outcome.replace("**", ""), ephemeral=True)
                    return
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("craft failed user=%s", uid)
            await ctx.send("Could not craft — try again.", ephemeral=True)
            return

        pack = outcome.pack
        series = outcome.series
        uses_note = ""
        if outcome.trainer_uses_remaining is not None:
            uses_note = (
                f"\n**{outcome.trainer_name}** has "
                f"**{outcome.trainer_uses_remaining}** craft use(s) left."
            )
        elif outcome.trainer_uses_remaining is None and outcome.trainer_name:
            uses_note = f"\n**{outcome.trainer_name}** was consumed (no uses left)."

        embed = discord.Embed(
            title="Pack crafted",
            description=(
                f"**{series.display_name}** — tier **{outcome.pack_tier_rarity}** "
                f"(from materials + **{outcome.trainer_name}**).\n"
                f"Pack ID: `{pack.public_id}`{uses_note}\n\n"
                f"Browse unopened packs with **`/packcolv`** (not the `/packv` shop list), "
                f"or view this one: `/packv card_ref:{pack.public_id}`."
            ),
            color=discord.Color.purple(),
        )
        if series.pack_art_url:
            embed.set_thumbnail(url=series.pack_art_url)

        view = CraftOpenPackView(self, owner_id=uid, pack_public_id=pack.public_id)
        msg = await ctx.send(embed=embed, view=view, ephemeral=True)
        await _reply_card_id_below(ctx, msg, pack.public_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CraftingCog(bot))
