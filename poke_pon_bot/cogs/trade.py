"""In-server trades — cards + Pokedollars (offer → Accept / Decline / Cancel)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.pending_trade import PendingTrade
from poke_pon_bot.services.instance_public_id import compact_public_id_for_line
from poke_pon_bot.services.trades import (
    MAX_TRADE_POKEDOLLARS,
    TRADE_OFFER_TTL_MINUTES,
    cancel_trade,
    decline_trade,
    delete_pending_as_initiator,
    execute_trade_accept,
    resolve_owned_instances,
    split_card_tokens,
)
from poke_pon_bot.services.wallet import WalletService, format_pokedollars

_LOG = logging.getLogger(__name__)


def _hybrid_ephemeral(ctx: commands.Context) -> bool:
    return False


async def _side_summary(session, instances: list, money: int) -> str:
    lines: list[str] = []
    for inst in instances:
        card = await session.get(Card, inst.card_id)
        nm = card.name if card else "Unknown"
        pid = compact_public_id_for_line(inst.public_id)
        lines.append(f"• **{nm}** `{pid}`")
    if money > 0:
        lines.append(f"• **{format_pokedollars(money)}**")
    if not lines:
        return "_Nothing_"
    return "\n".join(lines)


class TradeOfferView(discord.ui.View):
    """Partner: **Accept** / **Decline** — initiator: **Cancel**."""

    def __init__(
        self,
        *,
        bot: commands.Bot,
        wallet: WalletService,
        pending_id: int,
        initiator_id: int,
        partner_id: int,
    ) -> None:
        super().__init__(timeout=float(TRADE_OFFER_TTL_MINUTES * 60))
        self._bot = bot
        self._wallet = wallet
        self._pending_id = pending_id
        self._initiator_id = initiator_id
        self._partner_id = partner_id

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                e = msg.embeds[0] if msg.embeds else discord.Embed(title="Trade timed out")
                e2 = e.copy()
                foot = e2.footer.text or ""
                e2.set_footer(text=f"{foot} · Timed out" if foot else "Timed out")
                await msg.edit(embed=e2, view=self)
            except (discord.NotFound, discord.HTTPException):
                pass
        try:
            async with self._bot.async_session_factory() as session:
                pt = await session.get(PendingTrade, self._pending_id)
                if pt is not None:
                    await session.delete(pt)
                    await session.commit()
        except SQLAlchemyError:
            _LOG.exception("trade timeout cleanup pending %s", self._pending_id)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, row=0)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self._partner_id:
            await interaction.response.send_message("Only the invited player can accept.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            async with self._bot.async_session_factory() as session:
                err = await execute_trade_accept(
                    session,
                    self._wallet,
                    pending_id=self._pending_id,
                    accepting_user_id=interaction.user.id,
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("trade accept pending %s", self._pending_id)
            await interaction.followup.send("Could not complete the trade. Try again.", ephemeral=True)
            return
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        done = discord.Embed(
            title="Trade completed",
            description=f"<@{self._initiator_id}> ⇄ <@{self._partner_id}>",
        )
        await interaction.edit_original_response(embed=done, view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary, row=0)
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self._partner_id:
            await interaction.response.send_message("Only the invited player can decline.", ephemeral=True)
            return
        try:
            async with self._bot.async_session_factory() as session:
                err = await decline_trade(session, pending_id=self._pending_id, decliner_id=interaction.user.id)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("trade decline pending %s", self._pending_id)
            await interaction.response.send_message("Could not decline. Try again.", ephemeral=True)
            return
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        emb = discord.Embed(title="Trade declined", description=f"<@{self._partner_id}> declined.")
        await interaction.response.edit_message(embed=emb, view=None)

    @discord.ui.button(label="Cancel offer", style=discord.ButtonStyle.danger, row=0)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self._initiator_id:
            await interaction.response.send_message("Only whoever opened the trade can cancel.", ephemeral=True)
            return
        try:
            async with self._bot.async_session_factory() as session:
                err = await cancel_trade(session, pending_id=self._pending_id, canceller_id=interaction.user.id)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("trade cancel pending %s", self._pending_id)
            await interaction.response.send_message("Could not cancel. Try again.", ephemeral=True)
            return
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        emb = discord.Embed(title="Trade cancelled", description=f"<@{self._initiator_id}> cancelled the offer.")
        await interaction.response.edit_message(embed=emb, view=None)


class TradeCog(commands.Cog):
    """Slash **`/trade`** and chat **`trade`** for player trades."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wallet = WalletService()

    @commands.guild_only()
    @commands.hybrid_group(name="trade", invoke_without_command=True)
    async def trade_root(self, ctx: commands.Context) -> None:
        """Trade cards & Pokedollars — see **`trade offer`** or **`trade gift`**."""
        if ctx.invoked_subcommand is not None:
            return
        await ctx.send(
            "**`/trade offer`** (`ctrade offer`) — propose an exchange (**Card IDs**, commas/spaces). "
            "Set **`give_pokedollars`** / **`receive_pokedollars`** for ₽.\n"
            "**`/trade gift`** — give cards/money without asking for anything back.\n"
            "The invited player uses **Accept** / **Decline**; you can **Cancel offer**.",
            ephemeral=_hybrid_ephemeral(ctx),
        )

    async def _run_offer(
        self,
        ctx: commands.Context,
        partner: discord.Member,
        give_cards_raw: str | None,
        give_pokedollars: int,
        receive_cards_raw: str | None,
        receive_pokedollars: int,
        *,
        gift_mode: bool,
    ) -> None:
        ephe = _hybrid_ephemeral(ctx)
        if ctx.guild is None:
            await ctx.send("Use trades in a server text channel.", ephemeral=ephe)
            return
        if partner.id == ctx.author.id:
            await ctx.send("You can’t trade with yourself.", ephemeral=ephe)
            return
        if partner.bot:
            await ctx.send("Pick a human server member to trade with.", ephemeral=ephe)
            return

        if ctx.interaction:
            await ctx.defer(ephemeral=False)

        gm = max(0, min(int(give_pokedollars), MAX_TRADE_POKEDOLLARS))
        rm = max(0, min(int(receive_pokedollars), MAX_TRADE_POKEDOLLARS))

        if gift_mode:
            receive_cards_raw = None
            rm = 0

        give_toks = split_card_tokens(give_cards_raw)
        recv_toks = split_card_tokens(receive_cards_raw)

        initiator_has = bool(give_toks or gm > 0)
        partner_has = bool(recv_toks or rm > 0)
        if not initiator_has and not partner_has:
            await ctx.send("Offer something from **at least one** side (cards or **Pokedollars**).", ephemeral=ephe)
            return
        if gift_mode and not initiator_has:
            await ctx.send("In **`gift`** mode you must give at least one card or some **Pokedollars**.", ephemeral=ephe)
            return

        try:
            async with self.bot.async_session_factory() as session:
                give_inst, ge = await resolve_owned_instances(session, ctx.author.id, give_toks)
                if ge:
                    await ctx.send(ge, ephemeral=ephe)
                    return
                recv_inst, re_err = await resolve_owned_instances(session, partner.id, recv_toks)
                if re_err:
                    await ctx.send(f"They must own those cards: {re_err}", ephemeral=ephe)
                    return

                await delete_pending_as_initiator(session, ctx.author.id)

                expires = datetime.now(UTC) + timedelta(minutes=TRADE_OFFER_TTL_MINUTES)
                pt = PendingTrade(
                    guild_id=ctx.guild.id,
                    channel_id=ctx.channel.id,
                    message_id=None,
                    initiator_id=ctx.author.id,
                    partner_id=partner.id,
                    give_instance_ids=[i.id for i in give_inst],
                    give_pokedollars=gm,
                    receive_instance_ids=[i.id for i in recv_inst],
                    receive_pokedollars=rm,
                    expires_at=expires,
                )
                session.add(pt)
                await session.flush()

                init_give = await _side_summary(session, give_inst, gm)
                partner_give = await _side_summary(session, recv_inst, rm)

                emb = discord.Embed(
                    title="Trade offer",
                    description=(
                        f"{ctx.author.mention} ⇄ {partner.mention}\n\n"
                        f"{partner.mention}: **Accept** or **Decline**\n"
                        f"{ctx.author.mention}: **Cancel offer**"
                    ),
                )
                emb.add_field(name=f"{ctx.author.display_name} gives", value=init_give[:1024], inline=False)
                emb.add_field(name=f"{partner.display_name} gives", value=partner_give[:1024], inline=False)
                emb.set_footer(text=f"Expires {discord.utils.format_dt(expires, 'R')}")

                view = TradeOfferView(
                    bot=self.bot,
                    wallet=self._wallet,
                    pending_id=int(pt.id),
                    initiator_id=ctx.author.id,
                    partner_id=partner.id,
                )

                msg = await ctx.send(content=partner.mention, embed=emb, view=view)
                pt.message_id = msg.id
                await session.commit()
                view.message = msg
        except SQLAlchemyError:
            _LOG.exception("trade offer user %s -> %s", ctx.author.id, partner.id)
            await ctx.send("Could not save that trade offer.", ephemeral=ephe)

    @trade_root.command(name="offer")
    @app_commands.describe(
        partner="Who you’re trading with",
        give_cards="Your Card IDs (commas or spaces)",
        receive_cards="Their Card IDs you’re asking for",
        give_pokedollars="₽ you pay them when the trade completes",
        receive_pokedollars="₽ they pay you when the trade completes",
    )
    async def trade_offer(
        self,
        ctx: commands.Context,
        partner: discord.Member,
        give_cards: str | None = None,
        receive_cards: str | None = None,
        give_pokedollars: int = 0,
        receive_pokedollars: int = 0,
    ) -> None:
        await self._run_offer(
            ctx,
            partner,
            give_cards,
            give_pokedollars,
            receive_cards,
            receive_pokedollars,
            gift_mode=False,
        )

    @trade_root.command(name="gift")
    @app_commands.describe(
        partner="Who receives the gift",
        give_cards="Card IDs you’re giving",
        give_pokedollars="₽ you’re giving",
    )
    async def trade_gift(
        self,
        ctx: commands.Context,
        partner: discord.Member,
        give_cards: str | None = None,
        give_pokedollars: int = 0,
    ) -> None:
        await self._run_offer(
            ctx,
            partner,
            give_cards,
            give_pokedollars,
            None,
            0,
            gift_mode=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TradeCog(bot))
    _LOG.info("Loaded trade cog: slash `/trade offer`, `/trade gift`; chat `trade …`.")
