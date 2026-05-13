"""Wishlist cog — /wishlist to view, manage, and remove wishlisted cards."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.wishlist import UserWishlist
from poke_pon_bot.services.wishlist import (
    WISHLIST_CAP,
    remove_wishlist,
    user_wishlist_entries,
)

_LOG = logging.getLogger(__name__)

_PAGE_SIZE = 10


def _build_page_embed(
    entries: list[tuple[UserWishlist, Card]],
    *,
    page: int,
    total: int,
    user_mention: str,
) -> discord.Embed:
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    e = discord.Embed(
        title="⭐ Wishlist",
        description=f"{user_mention}'s wishlisted cards — **{total}** / **{WISHLIST_CAP}** slots used",
    )
    if not entries:
        e.add_field(name="Empty", value="Use the ⭐ button on `/colv` or `/cv` to wishlist cards.", inline=False)
        return e

    lines: list[str] = []
    start = page * _PAGE_SIZE
    for i, (wl, card) in enumerate(entries, start=start + 1):
        rarity = card.tcg_rarity or "—"
        lines.append(f"**{i}.** {card.name} · {card.set_name} · {rarity}")

    e.add_field(name="\u200b", value="\n".join(lines), inline=False)
    e.set_footer(text=f"Page {page + 1} / {total_pages}")
    return e


class WishlistPageView(discord.ui.View):
    """Paginated wishlist browser."""

    def __init__(
        self,
        *,
        session_factory,
        user_id: int,
        user_mention: str,
        total: int,
    ) -> None:
        super().__init__(timeout=300.0)
        self._session_factory = session_factory
        self._user_id = user_id
        self._user_mention = user_mention
        self._total = total
        self._page = 0
        self._max_page = max(0, (total - 1) // _PAGE_SIZE)

        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
        self._prev.callback = self._on_prev
        self._next.callback = self._on_next
        self.add_item(self._prev)
        self.add_item(self._next)
        self._sync_nav()

    def _sync_nav(self) -> None:
        self._prev.disabled = self._page <= 0
        self._next.disabled = self._page >= self._max_page

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._user_id:
            await interaction.response.send_message("This isn't your wishlist view.", ephemeral=True)
            return
        if self._page > 0:
            self._page -= 1
        await self._refresh(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._user_id:
            await interaction.response.send_message("This isn't your wishlist view.", ephemeral=True)
            return
        if self._page < self._max_page:
            self._page += 1
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        try:
            async with self._session_factory() as session:
                entries, self._total = await user_wishlist_entries(
                    session,
                    self._user_id,
                    limit=_PAGE_SIZE,
                    offset=self._page * _PAGE_SIZE,
                )
        except SQLAlchemyError:
            _LOG.exception("wishlist page fetch user=%s page=%s", self._user_id, self._page)
            await interaction.response.send_message("Could not load page. Try again.", ephemeral=True)
            return
        self._max_page = max(0, (self._total - 1) // _PAGE_SIZE)
        self._sync_nav()
        embed = _build_page_embed(
            entries,
            page=self._page,
            total=self._total,
            user_mention=self._user_mention,
        )
        await interaction.response.edit_message(embed=embed, view=self)


class WishlistCog(commands.Cog):
    """Card wishlist management."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="wishlist",
        aliases=["pcwishlist", "wl"],
        description="View your wishlisted cards — chat: pcwishlist / wl",
    )
    @app_commands.describe(member="Whose wishlist to view — omit for yours")
    async def wishlist_cmd(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        target = member or ctx.author
        user_id = target.id
        user_mention = target.mention

        try:
            async with self.bot.async_session_factory() as session:
                entries, total = await user_wishlist_entries(
                    session, user_id, limit=_PAGE_SIZE, offset=0,
                )
        except SQLAlchemyError:
            _LOG.exception("wishlist cmd user=%s", user_id)
            await ctx.send("Could not load wishlist. Try again.", ephemeral=False)
            return

        embed = _build_page_embed(entries, page=0, total=total, user_mention=user_mention)
        view = WishlistPageView(
            session_factory=self.bot.async_session_factory,
            user_id=ctx.author.id,
            user_mention=user_mention,
            total=total,
        )
        await ctx.send(embed=embed, view=view, ephemeral=False)

    @commands.hybrid_command(
        name="wishlistremove",
        aliases=["pcwishlistremove", "wlr"],
        description="Remove a card from your wishlist by name — chat: wlr",
    )
    @app_commands.describe(name="Card name (or part of it) to remove from your wishlist")
    async def wishlist_remove_cmd(
        self,
        ctx: commands.Context,
        *,
        name: str,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        user_id = ctx.author.id
        try:
            async with self.bot.async_session_factory() as session:
                stmt = (
                    select(UserWishlist, Card)
                    .join(Card, UserWishlist.card_id == Card.id)
                    .where(UserWishlist.discord_user_id == user_id)
                    .where(Card.name.ilike(f"%{name}%"))
                    .order_by(UserWishlist.created_at.desc())
                    .limit(5)
                )
                rows = (await session.execute(stmt)).all()

                if not rows:
                    await ctx.send(
                        f"No wishlisted card matching **{name}** found.",
                        ephemeral=True,
                    )
                    return

                if len(rows) == 1:
                    wl, card = rows[0]
                    await remove_wishlist(session, discord_user_id=user_id, card_id=card.id)
                    await ctx.send(
                        f"Removed **{card.name}** ({card.set_name}) from your wishlist.",
                        ephemeral=True,
                    )
                    return

                lines = [f"Multiple matches for **{name}** — be more specific:"]
                for wl, card in rows:
                    lines.append(f"• {card.name} · {card.set_name} · `{card.tcg_card_id}`")
                await ctx.send("\n".join(lines), ephemeral=True)

        except SQLAlchemyError:
            _LOG.exception("wishlist remove user=%s name=%s", user_id, name)
            await ctx.send("Could not remove. Try again.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WishlistCog(bot))
