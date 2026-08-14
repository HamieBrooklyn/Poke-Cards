"""Invest in live TCGPlayer market moves — only for cards you own."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.chat_commands import pp_alias, pp_chat_aliases
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.card_images import card_image_urls
from poke_pon_bot.services.card_market import (
    INVEST_MAX_PD,
    INVEST_MIN_PD,
    InvestmentNotFoundError,
    MarketUnavailableError,
    MarketQuote,
    NotInCollectionError,
    PositionView,
    buy_position,
    format_usd_cents,
    get_open_position,
    market_trend,
    invested_card_ids,
    list_positions,
    load_history,
    load_quote,
    refresh_quote,
    sell_position,
)
from poke_pon_bot.services.collection_search import list_collection_instance_ids
from poke_pon_bot.services.instance_public_id import normalize_public_id
from poke_pon_bot.services.market_chart import render_market_chart_png
from poke_pon_bot.services.wallet import InsufficientPokedollarsError, format_pokedollars

_LOG = logging.getLogger(__name__)


def _api_key(bot: commands.Bot) -> str | None:
    return getattr(getattr(bot, "settings", None), "tcg_api_key", None)


async def _load_owned_instance(
    session,
    instance_id: int,
    owner_id: int,
) -> tuple[UserCardInstance, Card] | None:
    inst = await session.get(UserCardInstance, instance_id)
    if inst is None or int(inst.discord_user_id) != int(owner_id):
        return None
    card = await session.get(Card, inst.card_id)
    if card is None:
        return None
    return inst, card


async def _load_owned_by_public_id(
    session,
    public_id: str,
    owner_id: int,
) -> tuple[UserCardInstance, Card] | None:
    n = normalize_public_id(public_id)
    if n is None:
        return None
    inst = (
        await session.execute(
            select(UserCardInstance).where(
                UserCardInstance.discord_user_id == owner_id,
                UserCardInstance.public_id == n,
            )
        )
    ).scalar_one_or_none()
    if inst is None:
        return None
    card = await session.get(Card, inst.card_id)
    if card is None:
        return None
    return inst, card


def _skeleton_embed(card: Card, *, note: str | None = None) -> discord.Embed:
    e = discord.Embed(title=card.name, color=0x5B8CFF)
    bits = [x for x in (note, "Loading live TCGPlayer…") if x]
    e.description = "\n".join(bits)
    _small, large = card_image_urls(card, web_public_url=None)
    e.set_thumbnail(url=_small or large)
    e.add_field(name="Set", value=f"{card.set_name}\n`{card.set_code}` #{card.collector_number}", inline=True)
    return e


def _owned_market_embed(
    card: Card,
    quote: MarketQuote,
    *,
    note: str | None = None,
    position: PositionView | None = None,
    history=None,
) -> discord.Embed:
    trend = market_trend(quote, history)
    color = 0x3DDC97 if trend.is_up else 0xE65A6E if trend.is_down else 0x8B93A7
    if position is not None:
        color = 0x3DDC97 if position.pnl >= 0 else 0xE65A6E
    e = discord.Embed(title=card.name, color=color)
    bits = [note] if note else []
    bits.append(f"**{trend.headline()}**")
    if position is not None:
        sign = "+" if position.pnl >= 0 else ""
        bits.append(
            f"**Position:** {format_pokedollars(position.current_value)}  "
            f"({sign}{format_pokedollars(position.pnl)} / {position.pnl_percent:+.1f}%)"
        )
    e.description = "\n".join(x for x in bits if x)
    _small, large = card_image_urls(card, web_public_url=None)
    e.set_thumbnail(url=_small or large)
    e.add_field(name="Live TCGPlayer", value=f"**{format_usd_cents(quote.usd_cents)}**", inline=True)
    e.add_field(
        name="Range",
        value=f"{format_usd_cents(quote.usd_low_cents)} – {format_usd_cents(quote.usd_high_cents)}",
        inline=True,
    )
    e.add_field(name="Set", value=f"{card.set_name}\n`{card.set_code}` #{card.collector_number}", inline=True)
    if position is not None:
        e.add_field(name="Invested", value=format_pokedollars(position.investment.pokedollars_in), inline=True)
        e.add_field(name="Entry USD", value=format_usd_cents(position.investment.entry_usd_cents), inline=True)
    e.set_footer(text="Invest only in cards you own · TCGPlayer via pokemontcg.io")
    return e


async def _chart_file(session, card: Card, quote: MarketQuote) -> discord.File | None:
    hist = await load_history(session, int(card.id))
    png = render_market_chart_png(
        hist,
        title=f"{card.name} · {format_usd_cents(quote.usd_cents)}",
        current_cents=quote.usd_cents,
        quote=quote,
    )
    return discord.File(png, filename=f"market-{int(card.id)}.png")


class BuyAmountModal(discord.ui.Modal, title="Invest pokedollars"):
    amount = discord.ui.TextInput(
        label="Amount (₽)",
        placeholder=f"{INVEST_MIN_PD}–{INVEST_MAX_PD}",
        min_length=1,
        max_length=8,
    )

    def __init__(self, view: "MarketColvView") -> None:
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.amount.value or "").replace(",", "").replace("₽", "").strip()
        try:
            amount = int(raw)
        except ValueError:
            await interaction.response.send_message("Enter a whole ₽ amount.", ephemeral=True)
            return
        await self._view._buy(interaction, amount)


class MarketColvView(discord.ui.View):
    def __init__(
        self,
        cog: "MarketCog",
        *,
        owner_id: int,
        instance_ids: list[int],
        index: int = 0,
        has_position: bool = False,
    ) -> None:
        super().__init__(timeout=600.0)
        self._cog = cog
        self._owner_id = owner_id
        self._instance_ids = instance_ids
        self._index = index
        self._gen = 0
        self._has_position = has_position
        self._sync()

    def _sync(self) -> None:
        self.prev_btn.disabled = self._index <= 0
        self.next_btn.disabled = self._index >= len(self._instance_ids) - 1
        self.sell_btn.disabled = not self._has_position

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("Not your market collection.", ephemeral=True)
            return
        if self._index > 0:
            self._index -= 1
        await self._refresh(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("Not your market collection.", ephemeral=True)
            return
        if self._index < len(self._instance_ids) - 1:
            self._index += 1
        await self._refresh(interaction)

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success, row=1)
    async def buy_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("Not your market collection.", ephemeral=True)
            return
        await interaction.response.send_modal(BuyAmountModal(self))

    @discord.ui.button(label="Sell", style=discord.ButtonStyle.danger, row=1)
    async def sell_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("Not your market collection.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self._cog.bot.async_session_factory() as session:
                row = await _load_owned_instance(
                    session, self._instance_ids[self._index], self._owner_id
                )
                if row is None:
                    await interaction.followup.send("That card is gone from your collection.", ephemeral=True)
                    return
                _inst, card = row
                inv = await get_open_position(
                    session, discord_user_id=self._owner_id, card_id=int(card.id)
                )
                if inv is None:
                    await interaction.followup.send("No open position on this printing.", ephemeral=True)
                    return
                view, bal = await sell_position(
                    session,
                    discord_user_id=self._owner_id,
                    investment_id=int(inv.id),
                    api_key=_api_key(self._cog.bot),
                )
                await session.commit()
        except InvestmentNotFoundError:
            await interaction.followup.send("That position is gone.", ephemeral=True)
            return
        except MarketUnavailableError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except SQLAlchemyError:
            _LOG.exception("marketcolv sell user=%s", self._owner_id)
            await interaction.followup.send("Database error — try again.", ephemeral=True)
            return
        self._has_position = False
        self._sync()
        sign = "+" if view.pnl >= 0 else ""
        await interaction.followup.send(
            f"Sold **{view.card.name}** for **{format_pokedollars(view.current_value)}** "
            f"({sign}{format_pokedollars(view.pnl)}). Balance {format_pokedollars(bal)}.",
            ephemeral=True,
        )
        await self._refresh(interaction, already_responded=True)

    async def _buy(self, interaction: discord.Interaction, amount: int) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            async with self._cog.bot.async_session_factory() as session:
                row = await _load_owned_instance(
                    session, self._instance_ids[self._index], self._owner_id
                )
                if row is None:
                    await interaction.followup.send("That card is gone from your collection.", ephemeral=True)
                    return
                _inst, card = row
                view = await buy_position(
                    session,
                    discord_user_id=self._owner_id,
                    card=card,
                    amount=amount,
                    api_key=_api_key(self._cog.bot),
                )
                await session.commit()
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except NotInCollectionError:
            await interaction.followup.send("You can only invest in cards you own.", ephemeral=True)
            return
        except InsufficientPokedollarsError:
            await interaction.followup.send("Not enough pokedollars.", ephemeral=True)
            return
        except MarketUnavailableError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except SQLAlchemyError:
            _LOG.exception("marketcolv buy user=%s", self._owner_id)
            await interaction.followup.send("Database error — try again.", ephemeral=True)
            return
        self._has_position = True
        self._sync()
        await interaction.followup.send(
            f"Invested **{format_pokedollars(amount)}** in **{view.card.name}** "
            f"at {format_usd_cents(view.quote.usd_cents)}. "
            f"Now worth **{format_pokedollars(view.current_value)}**.",
            ephemeral=True,
        )
        await self._refresh(interaction, already_responded=True)

    async def _refresh(
        self,
        interaction: discord.Interaction,
        *,
        already_responded: bool = False,
    ) -> None:
        self._gen += 1
        gen = self._gen
        self._sync()
        if not already_responded and not interaction.response.is_done():
            await interaction.response.defer()
        iid = self._instance_ids[self._index]
        async with self._cog.bot.async_session_factory() as session:
            row = await _load_owned_instance(session, iid, self._owner_id)
        if row is None:
            await interaction.edit_original_response(
                embed=discord.Embed(title="Card gone from collection"),
                view=self,
                attachments=[],
            )
            return
        _inst, card = row
        note = f"**{self._index + 1}** / **{len(self._instance_ids)}**"
        await interaction.edit_original_response(
            embed=_skeleton_embed(card, note=note),
            view=self,
            attachments=[],
        )
        if gen != self._gen:
            return
        try:
            embed, files, has_pos = await self._cog._owned_market_message(
                iid, owner_id=self._owner_id, note=note
            )
        except MarketUnavailableError as exc:
            if gen != self._gen:
                return
            fail = _skeleton_embed(card, note=note)
            fail.description = f"{note}\n{exc}"
            await interaction.edit_original_response(embed=fail, view=self, attachments=[])
            return
        if gen != self._gen:
            return
        self._has_position = has_pos
        self._sync()
        await interaction.edit_original_response(embed=embed, view=self, attachments=files or [])


class MarketCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.refresh_invested_quotes.start()

    def cog_unload(self) -> None:
        self.refresh_invested_quotes.cancel()

    @tasks.loop(minutes=20)
    async def refresh_invested_quotes(self) -> None:
        try:
            async with self.bot.async_session_factory() as session:
                ids = await invested_card_ids(session)
                for cid in ids:
                    card = await session.get(Card, cid)
                    if card is None:
                        continue
                    try:
                        await refresh_quote(session, card, api_key=_api_key(self.bot), force=True)
                    except (MarketUnavailableError, Exception):
                        _LOG.exception("market refresh card=%s", cid)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("market refresh loop")

    @refresh_invested_quotes.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _owned_market_message(
        self,
        instance_id: int,
        *,
        owner_id: int,
        note: str | None = None,
    ) -> tuple[discord.Embed, list[discord.File], bool]:
        from poke_pon_bot.services.card_market import _position_view

        async with self.bot.async_session_factory() as session:
            row = await _load_owned_instance(session, instance_id, owner_id)
            if row is None:
                return discord.Embed(title="Missing card"), [], False
            inst, card = row
            quote = await load_quote(session, card, api_key=_api_key(self.bot), allow_stale=True)
            inv = await get_open_position(session, discord_user_id=owner_id, card_id=int(card.id))
            position = _position_view(inv, card, quote) if inv is not None else None
            hist = await load_history(session, int(card.id))
            embed = _owned_market_embed(
                card,
                quote,
                note=note,
                position=position,
                history=hist,
            )
            if inst.public_id:
                embed.add_field(name="Card ID", value=f"`{inst.public_id}`", inline=True)
            chart = await _chart_file(session, card, quote)
            await session.commit()
        files: list[discord.File] = []
        if chart is not None:
            embed.set_image(url=f"attachment://market-{int(card.id)}.png")
            files.append(chart)
        return embed, files, position is not None

    @commands.hybrid_command(
        name="marketcolv",
        aliases=[pp_alias("marketcolv")],
        description="Flip your collection with TCGPlayer chart + buy/sell — chat: ppmarketcolv",
    )
    @app_commands.describe(
        card_id="Your **Card ID** for one copy — jump to that card",
        limit="Cap cards to flip, newest first (default 50, max 1000)",
    )
    async def marketcolv(
        self,
        ctx: commands.Context,
        card_id: str | None = None,
        limit: app_commands.Range[int, 1, 1000] | None = None,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        owner_id = ctx.author.id
        raw_id = (card_id or "").strip() or None
        if raw_id and normalize_public_id(raw_id) is None:
            if raw_id.isdigit() and limit is None:
                n = int(raw_id)
                if 1 <= n <= 1000:
                    limit = n
                    raw_id = None
            if raw_id:
                await ctx.send(
                    "That doesn’t look like a **Card ID** (16 url-safe chars or 32 hex). "
                    "Copy it from **`ppcolv`** or the collection page."
                )
                return
        start_index = 0
        try:
            async with self.bot.async_session_factory() as session:
                ids, total = await list_collection_instance_ids(
                    session,
                    discord_user_id=owner_id,
                    max_ids=limit,
                )
                if raw_id:
                    row = await _load_owned_by_public_id(session, raw_id, owner_id)
                    if row is None:
                        await ctx.send("You don’t have a copy with that **Card ID**.")
                        return
                    inst, _card = row
                    if inst.id in ids:
                        start_index = ids.index(inst.id)
                    else:
                        ids = [inst.id, *ids]
                        start_index = 0
                        total += 1
            if not ids:
                await ctx.send("Empty collection. Use **`ppcd`** to pull cards first.")
                return
            note = f"**{start_index + 1}** / **{len(ids)}**"
            if total > len(ids):
                note += f" of **{total}**"
            embed, files, has_pos = await self._owned_market_message(
                ids[start_index],
                owner_id=owner_id,
                note=note,
            )
        except MarketUnavailableError:
            await ctx.send("No live TCGPlayer market for that printing yet.")
            return
        except SQLAlchemyError:
            _LOG.exception("marketcolv user=%s", owner_id)
            await ctx.send("Could not load your collection market.")
            return
        view = MarketColvView(
            self,
            owner_id=owner_id,
            instance_ids=ids,
            index=start_index,
            has_position=has_pos,
        )
        await ctx.send(embed=embed, view=view, files=files)

    @commands.hybrid_command(
        name="marketinveststatus",
        aliases=[*pp_chat_aliases("marketinveststatus", "mis")],
        description="List your open card investments — chat: ppmarketinveststatus / ppmis",
    )
    async def marketinveststatus(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        try:
            async with self.bot.async_session_factory() as session:
                positions = await list_positions(
                    session,
                    discord_user_id=ctx.author.id,
                    api_key=_api_key(self.bot),
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("marketinveststatus user=%s", ctx.author.id)
            await ctx.send("Could not load your investments.")
            return
        if not positions:
            await ctx.send(
                "No open investments. Open a card with **`ppmarketcolv`** and tap **Buy**."
            )
            return
        embed = discord.Embed(
            title="Your investments",
            color=0x3DDC97,
            description=f"**{len(positions)}** open position" + ("" if len(positions) == 1 else "s"),
        )
        shown = positions[:25]
        for view in shown:
            sign = "+" if view.pnl >= 0 else ""
            embed.add_field(
                name=view.card.name[:256],
                value=(
                    f"{view.card.set_code} #{view.card.collector_number}\n"
                    f"{format_pokedollars(view.investment.pokedollars_in)} → "
                    f"**{format_pokedollars(view.current_value)}** "
                    f"({sign}{format_pokedollars(view.pnl)} / {view.pnl_percent:+.1f}%)\n"
                    f"Live {format_usd_cents(view.quote.usd_cents)}"
                ),
                inline=False,
            )
        if len(positions) > 25:
            embed.set_footer(text=f"Showing 25 of {len(positions)}")
        else:
            embed.set_footer(text="Sell from ppmarketcolv on that printing")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MarketCog(bot))
    _LOG.info("Loaded market cog: `/marketcolv`, `/marketinveststatus`.")
