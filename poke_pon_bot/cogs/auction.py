"""Player auctions — timed listings, bids, automatic settlement."""

from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from poke_pon_bot.context_reply import reply_target_user_id
from poke_pon_bot.models.auction import CardAuction
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.auction_runtime import (
    auction_amount_display,
    format_auction_time_remaining,
    normalize_auction_bid_currency,
    parse_auction_duration_minutes,
    place_auction_bid,
    resolve_auction_id_for_bid,
    settle_due_auctions,
)
from poke_pon_bot.services.notification_delivery import schedule_outbid_alert
from poke_pon_bot.services.auction_search import any_auction_filter_set, search_auctions
from poke_pon_bot.services.combat_deck import strip_instances_from_deck
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.crystal_sinks import (
    AUCTION_SPOTLIGHT_CRYSTAL_COST,
    apply_spotlight_to_auction,
)
from poke_pon_bot.services.grading import format_grade_slab_badge
from poke_pon_bot.services.crystals import CrystalsService, format_crystals
from poke_pon_bot.services.instance_public_id import compact_public_id_for_line, normalize_public_id
from poke_pon_bot.services.wishlist_market_alerts import schedule_wishlist_auction_alert
from poke_pon_bot.services.trades import MAX_TRADE_POKEDOLLARS
from poke_pon_bot.services.wallet import WalletService, format_pokedollars

_LOG = logging.getLogger(__name__)

MAX_AUCTION_PRICE = MAX_TRADE_POKEDOLLARS
MIN_AUCTION_PRICE = 1


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _hybrid_ephemeral(ctx: commands.Context) -> bool:
    return False


class ListCardAuctionModal(discord.ui.Modal):
    card_ref = discord.ui.TextInput(
        label="Card ID",
        placeholder="Your Card ID (grey block from collection)",
        style=discord.TextStyle.short,
        required=True,
        max_length=48,
    )
    price = discord.ui.TextInput(
        label="Starting price (Pokedollars)",
        placeholder="e.g. 1500",
        style=discord.TextStyle.short,
        required=True,
        max_length=12,
    )
    duration = discord.ui.TextInput(
        label="How long the auction runs",
        placeholder="120 · 2h · 1d · 30 minutes · 2 hours (min 5m)",
        style=discord.TextStyle.short,
        required=True,
        max_length=48,
    )

    def __init__(self, bot: commands.Bot, *, guild_id: int | None) -> None:
        super().__init__(title="Auction — list a card")
        self._bot = bot
        self._guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        raw_card = (self.card_ref.value or "").strip()
        raw_price = (self.price.value or "").strip().replace(",", "")
        raw_dur = (self.duration.value or "").strip()
        dur_minutes, dur_err = parse_auction_duration_minutes(raw_dur)
        if dur_err is not None:
            await interaction.followup.send(dur_err, ephemeral=True)
            return
        assert dur_minutes is not None
        ends_at = discord.utils.utcnow() + timedelta(minutes=dur_minutes)
        n = normalize_public_id(raw_card)
        if n is None:
            await interaction.followup.send(
                "That doesn't look like a **Card ID** (16 characters, or 32 hex for older cards).",
                ephemeral=True,
            )
            return
        if not raw_price.isdigit():
            await interaction.followup.send(
                "**Starting price** must be a whole number of Pokedollars (digits only).",
                ephemeral=True,
            )
            return
        amount = int(raw_price)
        if amount < MIN_AUCTION_PRICE or amount > MAX_AUCTION_PRICE:
            await interaction.followup.send(
                f"Price must be between **{format_pokedollars(MIN_AUCTION_PRICE)}** "
                f"and **{format_pokedollars(MAX_AUCTION_PRICE)}**.",
                ephemeral=True,
            )
            return
        listing_id = 0
        try:
            async with self._bot.async_session_factory() as session:
                row = await session.execute(
                    select(UserCardInstance, Card)
                    .join(Card, UserCardInstance.card_id == Card.id)
                    .where(UserCardInstance.discord_user_id == uid, UserCardInstance.public_id == n)
                    .limit(1),
                )
                first = row.first()
                if first is None:
                    await interaction.followup.send(
                        "You don't own a card with that **Card ID**.",
                        ephemeral=True,
                    )
                    return
                inst, card = first
                dup = await session.scalar(
                    select(CardAuction.id).where(
                        CardAuction.instance_id == inst.id,
                        CardAuction.status == "active",
                    ),
                )
                if dup is not None:
                    await interaction.followup.send(
                        "That card already has an active auction listing.",
                        ephemeral=True,
                    )
                    return
                await strip_instances_from_deck(session, uid, {inst.id})
                listing_row = CardAuction(
                    seller_discord_id=uid,
                    guild_id=self._guild_id,
                    instance_id=inst.id,
                    price_pokedollars=amount,
                    ends_at=ends_at,
                    status="active",
                )
                session.add(listing_row)
                pid_line = compact_public_id_for_line(inst.public_id)
                card_name = card.name
                try:
                    await session.flush()
                    listing_id = listing_row.id
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    await interaction.followup.send(
                        "That card already has a listing (try another copy).",
                        ephemeral=True,
                    )
                    return
            schedule_wishlist_auction_alert(
                self._bot,
                seller_id=uid,
                auction_id=listing_id,
                catalog_card_id=int(inst.card_id),
                card_name=card_name,
                price=amount,
                currency=listing_row.bid_currency,
            )
            time_note = format_auction_time_remaining(ends_at)
            await interaction.followup.send(
                f"Listed **{card_name}** `{pid_line}` · min bid **{format_pokedollars(amount)}** · **{time_note}** "
                f"(listing **`{listing_id}`**).\n"
                "Others can **`/auction search`** and **`/auction bid`** with listing **`"
                f"{listing_id}`** or this card’s **Card ID**.\n"
                f"Feature it in search anytime with **`/auction spotlight {listing_id}`** "
                f"({format_crystals(AUCTION_SPOTLIGHT_CRYSTAL_COST)}, 24h).",
                ephemeral=True,
            )
        except SQLAlchemyError:
            _LOG.exception("auction list modal user %s", uid)
            await interaction.followup.send(
                "Could not save listing. Try again.",
                ephemeral=True,
            )


class AuctionCog(commands.Cog):
    """Timed auctions: modal listing, hybrid search + bid, background settlement."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wallet = WalletService()
        self._crystals = CrystalsService()

    async def cog_load(self) -> None:
        self._auction_settler.start()

    async def cog_unload(self) -> None:
        self._auction_settler.cancel()

    @tasks.loop(seconds=45)
    async def _auction_settler(self) -> None:
        try:
            n = await settle_due_auctions(
                self.bot.async_session_factory, self._wallet, self._crystals
            )
            if n:
                _LOG.info("Settled %s auction(s).", n)
        except SQLAlchemyError:
            _LOG.exception("auction settlement loop")

    @_auction_settler.before_loop
    async def _auction_settler_before(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_group(
        name="auction",
        description="Create listings, search, or bid on card auctions",
        invoke_without_command=False,
    )
    async def auction_group(self, ctx: commands.Context) -> None:
        """Parent group — requires a subcommand (slash picks **create** / **search** / **bid**)."""
        pass

    @auction_group.error
    async def auction_group_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingSubcommand):
            await ctx.send(
                "Use **`/auction create`**, **`/auction search`**, **`/auction bid`**, or **`/auction spotlight`**. "
                "In chat: **`ca create`**, **`ca search`**, **`ca bid`**, **`ca spotlight`**.",
                ephemeral=_hybrid_ephemeral(ctx),
            )
            return
        raise error

    @auction_group.command(
        name="create",
        description="List a card for auction (opens a form)",
        aliases=["ac", "alc"],
    )
    async def auction_create_cmd(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            gid = ctx.interaction.guild_id
            await ctx.interaction.response.send_modal(ListCardAuctionModal(self.bot, guild_id=gid))
            return
        await ctx.send(
            "**Listing form:** use **`/auction create`** from the **`/`** menu (Discord only sends modals from slash).\n"
            "**In chat**: type **`auction search`** or **`auction bid`**; open the listing form with slash **`/auction create`**.\n"
            "Modal: **Card ID**, **starting bid**, **duration** (e.g. **`2h`**, **`30 minutes`**).",
            ephemeral=_hybrid_ephemeral(ctx),
        )

    @auction_group.command(
        name="bid",
        description="Place a bid on an active auction listing",
        aliases=["ab", "abd"],
    )
    @app_commands.describe(
        listing_or_card_id="Listing number from search (e.g. 12) or the card’s Card ID",
        amount="Your bid (must beat the current high bid / meet minimum)",
    )
    async def auction_bid_cmd(
        self,
        ctx: commands.Context,
        listing_or_card_id: str,
        amount: app_commands.Range[int, MIN_AUCTION_PRICE, MAX_AUCTION_PRICE],
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        await settle_due_auctions(
            self.bot.async_session_factory, self._wallet, self._crystals
        )
        resolved_listing_id: int | None = None
        prev_bidder: int | None = None
        card_name = "Card"
        bid_currency = "pokedollars"
        try:
            async with self.bot.async_session_factory() as session:
                resolved_listing_id, resolve_err = await resolve_auction_id_for_bid(session, listing_or_card_id)
                if resolve_err is not None:
                    await ctx.send(resolve_err, ephemeral=ephe)
                    return
                assert resolved_listing_id is not None
                auc = await session.get(CardAuction, int(resolved_listing_id))
                if auc is not None:
                    prev_bidder = auc.high_bidder_discord_id
                    bid_currency = normalize_auction_bid_currency(auc.bid_currency) or bid_currency
                    inst = await session.get(UserCardInstance, auc.instance_id)
                    if inst is not None:
                        card = await session.get(Card, inst.card_id)
                        if card is not None:
                            card_name = str(card.name)
                err = await place_auction_bid(
                    session,
                    self._wallet,
                    self._crystals,
                    auction_id=int(resolved_listing_id),
                    bidder_discord_id=uid,
                    amount=int(amount),
                )
                if err is not None:
                    await ctx.send(err, ephemeral=ephe)
                    return
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("auction bid user %s raw %r", uid, listing_or_card_id)
            await ctx.send("Could not place bid. Try again.", ephemeral=ephe)
            return
        if prev_bidder is not None and int(prev_bidder) != uid:
            base = (self.bot.settings.web_frontend_url or "").rstrip("/")
            auctions_url = f"{base}/auctions/" if base else ""
            schedule_outbid_alert(
                self.bot,
                outbid_user_id=int(prev_bidder),
                auction_id=int(resolved_listing_id),
                card_name=card_name,
                new_amount_label=auction_amount_display(int(amount), bid_currency),
                auctions_url=auctions_url,
            )
        await ctx.send(
            f"You're the high bidder at **{format_pokedollars(int(amount))}** on listing **`{resolved_listing_id}`**.",
            ephemeral=ephe,
        )

    @auction_group.command(
        name="spotlight",
        description="Feature your active listing in search for 24h (12 💎)",
        aliases=["asp"],
    )
    @app_commands.describe(
        listing_or_card_id="Your listing number from search or the card’s Card ID",
    )
    async def auction_spotlight_cmd(
        self,
        ctx: commands.Context,
        listing_or_card_id: str,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        uid = ctx.author.id
        await settle_due_auctions(
            self.bot.async_session_factory, self._wallet, self._crystals
        )
        try:
            async with self.bot.async_session_factory() as session:
                aid, resolve_err = await resolve_auction_id_for_bid(session, listing_or_card_id)
                if resolve_err is not None:
                    await ctx.send(resolve_err, ephemeral=True)
                    return
                assert aid is not None
                outcome = await apply_spotlight_to_auction(
                    session,
                    self._crystals,
                    seller_discord_id=uid,
                    auction_id=int(aid),
                )
                if not outcome.ok:
                    await ctx.send(outcome.error or "Could not spotlight.", ephemeral=True)
                    return
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("auction spotlight user %s raw %r", uid, listing_or_card_id)
            await ctx.send("Could not apply spotlight. Try again.", ephemeral=True)
            return

        until_note = ""
        if outcome.spotlight_until is not None:
            until_note = f" · **{format_auction_time_remaining(outcome.spotlight_until)}**"
        await ctx.send(
            f"✨ Listing **`{outcome.auction_id}`** is **spotlighted** for 24h "
            f"({format_crystals(AUCTION_SPOTLIGHT_CRYSTAL_COST)}){until_note}. "
            "It will appear at the top of **`/auction search`** while active.",
            ephemeral=True,
        )

    @auction_group.command(
        name="search",
        description="Search active auctions",
        aliases=["as", "ahs", "find"],
    )
    @app_commands.describe(
        scope="Limit to this server or include every server (default: server when in a guild)",
        name="Card name contains",
        rarity="Printed rarity contains",
        pokedex="Pokédex # filter",
        seller="Only this member’s listings (omit yours or anyone)",
        limit="Max rows (1–25)",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="This server", value="server"),
            app_commands.Choice(name="All servers (global)", value="global"),
        ]
    )
    async def auction_search_cmd(
        self,
        ctx: commands.Context,
        scope: app_commands.Choice[str] | None = None,
        name: str | None = None,
        rarity: str | None = None,
        pokedex: int | None = None,
        seller: discord.Member | None = None,
        limit: app_commands.Range[int, 1, 25] | None = None,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        await settle_due_auctions(
            self.bot.async_session_factory, self._wallet, self._crystals
        )
        if seller is not None and seller.bot:
            await ctx.send("Use a **member**, not a bot.", ephemeral=ephe)
            return

        # Default to **server** scope when invoked inside a guild; **global** in DM (no guild to scope to).
        scope_value = scope.value if scope is not None else ("server" if ctx.guild is not None else "global")
        guild_filter: int | None = None
        if scope_value == "server":
            if ctx.guild is None:
                await ctx.send(
                    "**Server scope** only works inside a server. Use **`scope: All servers`** in DMs.",
                    ephemeral=ephe,
                )
                return
            guild_filter = int(ctx.guild.id)

        reply_seller = await reply_target_user_id(self.bot, ctx)
        seller_id = int(seller.id) if seller is not None else reply_seller
        if not any_auction_filter_set(
            name_contains=name,
            rarity_contains=rarity,
            pokedex=pokedex,
            seller_discord_id=seller_id,
            guild_id=guild_filter,
        ):
            await ctx.send(
                "Add at least one filter (**`name`**, **`rarity`**, **`pokedex`**, **`seller`**) "
                "or pick **`scope: This server`** to browse this server's listings.",
                ephemeral=ephe,
            )
            return
        lim = int(limit) if limit is not None else 20
        try:
            async with self.bot.async_session_factory() as session:
                rows, total = await search_auctions(
                    session,
                    name_contains=name,
                    rarity_contains=rarity,
                    pokedex=pokedex,
                    seller_discord_id=seller_id,
                    guild_id=guild_filter,
                    page_limit=lim,
                    page_offset=0,
                )
        except SQLAlchemyError:
            _LOG.exception("auction search")
            await ctx.send("Could not search auctions. Try again.", ephemeral=ephe)
            return
        if total == 0:
            scope_note = "this server" if guild_filter is not None else "any server"
            await ctx.send(f"No active listings on {scope_note}.", ephemeral=ephe)
            return
        lines: list[str] = []
        for auc, inst, card in rows:
            pid = compact_public_id_for_line(inst.public_id)
            r = card.tcg_rarity or "?"
            time_left = format_auction_time_remaining(auc.ends_at)
            if auc.high_bid_pokedollars is not None:
                nxt = int(auc.high_bid_pokedollars) + 1
                if nxt > MAX_AUCTION_PRICE:
                    bid_note = f"high **{format_pokedollars(auc.high_bid_pokedollars)}** *(max bid)*"
                else:
                    bid_note = (
                        f"high **{format_pokedollars(auc.high_bid_pokedollars)}** · "
                        f"min raise **{format_pokedollars(nxt)}**"
                    )
            else:
                bid_note = f"no bids yet · min **{format_pokedollars(auc.price_pokedollars)}**"
            grade = int(inst.grade) if inst.grade is not None else None
            from poke_pon_bot.services.crystal_sinks import auction_spotlight_active

            spot = " ✨" if auction_spotlight_active(auc) else ""
            lines.append(
                f"• **`{auc.id}`**{spot} **{card.name}** — *{r}* · {card.set_name} `#{card.collector_number}`"
                f"{format_grade_slab_badge(grade, enchantment_code=getattr(inst, 'grade_enchantment', None))} · "
                f"{bid_note} · **{time_left}** · seller <@{auc.seller_discord_id}> · `{pid}`",
            )
        rest = total - len(rows)
        extra = f"\n_…**{rest}** more listing(s). Narrow filters._" if rest > 0 else ""
        seller_note = f"Seller **<@{seller_id}>** · " if seller_id is not None else ""
        scope_label = "this server" if guild_filter is not None else "all servers"
        header = (
            f"**Auctions** — {seller_note}**{len(rows)}** of **{total}** active on **{scope_label}** "
            f"(newest first)\n"
        )
        await ctx.send(_truncate(header + "\n".join(lines) + extra, 2000), ephemeral=ephe)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AuctionCog(bot))
    _LOG.info("Loaded auction cog: `/auction create`, `/auction search`, `/auction bid`, `/auction spotlight`.")
