"""Grade owned copies — PSA-style slab view and crystal rerolls."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.chat_commands import pp_alias
from poke_pon_bot.cogs.gacha import (
    _CardIdReplyBinding,
    _collection_view_embed,
    _edit_card_id_reply,
    _hybrid_ephemeral,
    _load_instance_by_public_id,
    _reply_card_id_below,
)
from poke_pon_bot.services.collection_sell import collection_sell_block_reason
from poke_pon_bot.services.crystals import CrystalsService, format_crystals
from poke_pon_bot.services.grading import (
    GRADE_CRYSTAL_COST,
    build_grade_preview,
    remove_grade,
    roll_grade_for_instance,
)
from poke_pon_bot.services.grade_slab import render_graded_slab_png
from poke_pon_bot.services.instance_public_id import normalize_public_id

_LOG = logging.getLogger(__name__)


def _grading_embed_note(preview) -> str:
    idx = preview.copy_index
    bits = [
        f"**Global copy rank:** **#{idx.copy_index:,}** of **{idx.total_copies:,}** for this printing "
        f"(earlier copies grade better on average).",
        f"**Roll / reroll:** {format_crystals(GRADE_CRYSTAL_COST)}",
    ]
    if preview.has_grade:
        bits.insert(
            0,
            f"**Grade:** **{preview.grade}** — **{preview.grade_label}**",
        )
    else:
        bits.insert(0, "**Not graded yet** — roll to seal this copy in a slab.")
    return "\n".join(bits)


class GradeCardView(_CardIdReplyBinding, discord.ui.View):
    def __init__(
        self,
        cog: GradingCog,
        *,
        owner_id: int,
        instance_id: int,
        public_id: str,
        has_grade: bool,
    ) -> None:
        super().__init__(timeout=600.0)
        self._init_card_id_reply()
        self._cog = cog
        self._owner_id = owner_id
        self._instance_id = instance_id
        self._public_id = public_id
        self._has_grade = has_grade
        self.message: discord.Message | None = None

        roll_lbl = (
            f"Reroll grade ({format_crystals(GRADE_CRYSTAL_COST)})"
            if has_grade
            else f"Roll grade ({format_crystals(GRADE_CRYSTAL_COST)})"
        )
        self._roll_btn = discord.ui.Button(
            label=roll_lbl,
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self._roll_btn.callback = self._on_roll
        self.add_item(self._roll_btn)

        if has_grade:
            rem = discord.ui.Button(
                label="Remove grade",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            rem.callback = self._on_remove
            self.add_item(rem)

    async def _refresh_message(self, interaction: discord.Interaction) -> None:
        async with self._cog.bot.async_session_factory() as session:
            row = await _load_instance_by_public_id(
                session, self._public_id, self._owner_id,
            )
            if row is None:
                await interaction.followup.send(
                    "That copy is no longer in your collection.",
                    ephemeral=True,
                )
                return
            inst, card = row
            preview = await build_grade_preview(session, inst)

        embed = _collection_view_embed(
            inst,
            card,
            rank_note=_grading_embed_note(preview),
        )
        view = GradeCardView(
            self._cog,
            owner_id=self._owner_id,
            instance_id=inst.id,
            public_id=inst.public_id,
            has_grade=inst.grade is not None,
        )

        files: list[discord.File] = []
        if inst.grade is not None:
            png = await render_graded_slab_png(
                card,
                grade=int(inst.grade),
                copy_index=preview.copy_index.copy_index,
                total_copies=preview.copy_index.total_copies,
                cert_suffix=inst.public_id,
            )
            if png is not None:
                embed.set_image(url="attachment://slab.png")
                files.append(discord.File(png, filename="slab.png"))
            else:
                embed.set_image(url=card.image_large_url or card.image_small_url)

        await interaction.edit_original_response(
            embed=embed,
            attachments=files,
            view=view,
        )
        view.message = interaction.message
        view._card_id_reply = getattr(self, "_card_id_reply", None)
        await _edit_card_id_reply(view._card_id_reply, inst.public_id)

    async def _on_roll(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "Only the owner can grade this copy.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        crystals = CrystalsService()
        try:
            async with self._cog.bot.async_session_factory() as session:
                outcome = await roll_grade_for_instance(
                    session,
                    crystals,
                    discord_user_id=self._owner_id,
                    instance_id=self._instance_id,
                )
                if not outcome.ok:
                    await interaction.followup.send(outcome.error or "Could not grade.", ephemeral=True)
                    return
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("grade roll user=%s inst=%s", self._owner_id, self._instance_id)
            await interaction.followup.send("Database error — try again.", ephemeral=True)
            return

        note = (
            f"**New grade:** **{outcome.grade}** — **{outcome.grade_label}**\n"
            f"Balance: {format_crystals(outcome.new_crystal_balance or 0)}"
        )
        await interaction.followup.send(note, ephemeral=True)
        await self._refresh_message(interaction)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "Only the owner can remove the grade.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            async with self._cog.bot.async_session_factory() as session:
                err = await remove_grade(
                    session,
                    discord_user_id=self._owner_id,
                    instance_id=self._instance_id,
                )
                if err:
                    await interaction.followup.send(err, ephemeral=True)
                    return
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("grade remove user=%s inst=%s", self._owner_id, self._instance_id)
            await interaction.followup.send("Database error — try again.", ephemeral=True)
            return

        await interaction.followup.send("Grade removed — showing the raw card again.", ephemeral=True)
        await self._refresh_message(interaction)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


class GradingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="grade",
        aliases=[pp_alias("grade")],
        description="View or roll a PSA-style grade for one of your copies",
    )
    @app_commands.describe(
        card_ref="Your **Card ID** for that copy (same as **`cv c`** **card_ref**)",
    )
    async def grade_card(self, ctx: commands.Context, card_ref: str) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        ref = (card_ref or "").strip()
        if not ref:
            await ctx.send(
                "Pass your **Card ID** — same as **`/cv c`** **card_ref**.",
                ephemeral=ephe,
            )
            return
        if normalize_public_id(ref) is None:
            await ctx.send(
                "That doesn’t look like a **Card ID** (16 url-safe chars or 32 hex).",
                ephemeral=ephe,
            )
            return

        try:
            async with self.bot.async_session_factory() as session:
                row = await _load_instance_by_public_id(session, ref, uid)
                if row is None:
                    await ctx.send("You don’t have a copy with that **Card ID**.", ephemeral=ephe)
                    return
                inst, card = row
                blocked = await collection_sell_block_reason(
                    session, discord_user_id=uid, instance_id=inst.id,
                )
                if blocked:
                    await ctx.send(blocked, ephemeral=ephe)
                    return
                preview = await build_grade_preview(session, inst)
        except SQLAlchemyError:
            _LOG.exception("grade view user=%s ref=%s", uid, ref)
            await ctx.send("Could not load that copy.", ephemeral=ephe)
            return

        embed = _collection_view_embed(
            inst,
            card,
            rank_note=_grading_embed_note(preview),
        )
        view = GradeCardView(
            self,
            owner_id=uid,
            instance_id=inst.id,
            public_id=inst.public_id,
            has_grade=inst.grade is not None,
        )

        files: list[discord.File] = []
        if inst.grade is not None:
            png = await render_graded_slab_png(
                card,
                grade=int(inst.grade),
                copy_index=preview.copy_index.copy_index,
                total_copies=preview.copy_index.total_copies,
                cert_suffix=inst.public_id,
            )
            if png is not None:
                embed.set_image(url="attachment://slab.png")
                files.append(discord.File(png, filename="slab.png"))
        else:
            embed.set_image(url=card.image_large_url or card.image_small_url)

        if files:
            msg = await ctx.send(embed=embed, view=view, files=files, ephemeral=ephe)
        else:
            msg = await ctx.send(embed=embed, view=view, ephemeral=ephe)
        view.message = msg
        view.bind_card_id_reply(await _reply_card_id_below(msg, inst.public_id))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GradingCog(bot))
    _LOG.info("Loaded grading cog: `/grade`.")
