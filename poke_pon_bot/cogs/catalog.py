"""Search and browse the global TCG card catalog (imported sets)."""

from __future__ import annotations

import logging

import discord
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.services.wishlist import is_wishlisted
from poke_pon_bot.ui.wishlist_toggle import WishlistToggleButton

_LOG = logging.getLogger(__name__)


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _catalog_view_embed(
    card: Card,
    *,
    rank_note: str | None = None,
) -> discord.Embed:
    """Single catalog card (not tied to a user's inventory)."""
    e = discord.Embed(title=card.name)
    if rank_note:
        e.description = rank_note
    from poke_pon_bot.services.card_images import card_image_urls

    _small, _large = card_image_urls(card, web_public_url=None)
    e.set_image(url=_large or _small)
    e.add_field(
        name="Set",
        value=f"{card.set_name}\n`{card.set_code}`",
        inline=True,
    )
    e.add_field(name="Card #", value=f"`{card.collector_number}`", inline=True)
    e.add_field(
        name="Printed rarity",
        value=card.tcg_rarity or "—",
        inline=True,
    )
    if card.supertype:
        e.add_field(name="Supertype", value=card.supertype, inline=True)
    if card.hp:
        e.add_field(name="HP", value=card.hp, inline=True)
    if card.dex_numbers:
        e.add_field(
            name="Pokédex #",
            value=", ".join(str(n) for n in card.dex_numbers),
            inline=False,
        )
    e.set_footer(text=f"Catalog · ID `{card.tcg_card_id}`")
    return e


async def _get_card(
    session: AsyncSession,
    card_id: int,
) -> Card | None:
    return await session.get(Card, card_id)


class CatalogBrowseView(discord.ui.View):
    """◀ / ▶ through catalog search results (card ids, same order as search)."""

    def __init__(
        self,
        *,
        session_factory,
        owner_id: int,
        card_ids: list[int],
        search_total: int,
        first_wishlisted: bool = False,
    ) -> None:
        if not card_ids:
            msg = "card_ids must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=600.0)
        self._session_factory = session_factory
        self._owner_id = owner_id
        self._card_ids = card_ids
        self._search_total = search_total
        self._index = 0
        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
        self._prev.callback = self._on_prev
        self._next.callback = self._on_next
        self.add_item(self._prev)
        self.add_item(self._next)
        self._wish_btn = WishlistToggleButton(
            session_factory=session_factory,
            card_id=int(card_ids[0]),
            viewer_id=owner_id,
            wishlisted=first_wishlisted,
            row=0,
        )
        self.add_item(self._wish_btn)
        self._sync_nav()

    @staticmethod
    def _note(index: int, page_size: int, search_total: int) -> str:
        if search_total == page_size:
            return (
                f"Catalog match **#{index + 1}** of **{search_total}** "
                f"(all matches shown; order: internal id)."
            )
        return (
            f"Catalog match **#{index + 1}** of **{page_size}** in this page — "
            f"**{search_total}** total matches; **slot {page_size + 1}** or higher: "
            f"one card, no **◀▶**; narrow filters to browse (order: internal id)."
        )

    def set_index(self, index: int, *, wishlisted: bool | None = None) -> None:
        self._index = max(0, min(index, len(self._card_ids) - 1))
        if wishlisted is not None:
            self._wish_btn.update(
                card_id=int(self._card_ids[self._index]),
                wishlisted=wishlisted,
            )
        self._sync_nav()

    def _sync_nav(self) -> None:
        n = len(self._card_ids)
        if n <= 1:
            self._prev.disabled = True
            self._next.disabled = True
        else:
            self._prev.disabled = self._index <= 0
            self._next.disabled = self._index >= n - 1

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This isn’t your catalog browser.",
                ephemeral=True,
            )
            return
        if self._index > 0:
            self._index -= 1
        await self._update(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This isn’t your catalog browser.",
                ephemeral=True,
            )
            return
        if self._index < len(self._card_ids) - 1:
            self._index += 1
        await self._update(interaction)

    async def _update(self, interaction: discord.Interaction) -> None:
        cid = self._card_ids[self._index]
        try:
            async with self._session_factory() as session:
                card = await _get_card(session, cid)
                if card is None:
                    await interaction.response.send_message(
                        "That card is no longer in the catalog (try re-running sync).",
                        ephemeral=True,
                    )
                    return
                wishlisted = await is_wishlisted(
                    session, discord_user_id=self._owner_id, card_id=card.id
                )
        except SQLAlchemyError:
            _LOG.exception("catalog browse card %s", cid)
            await interaction.response.send_message(
                "Could not load that card. Try again.",
                ephemeral=True,
            )
            return
        note = self._note(self._index, len(self._card_ids), self._search_total)
        self._wish_btn.update(card_id=card.id, wishlisted=wishlisted)
        self._sync_nav()
        await interaction.response.edit_message(
            embed=_catalog_view_embed(card, rank_note=note),
            view=self,
        )


def _search_kwargs(
    name: str | None,
    rarity: str | None,
    pokedex: int | None,
    set_code: str | None,
    set_name: str | None,
    supertype: str | None,
    rarity_tier: str | None,
    slot: int | None,
    tcg_card_id: str | None = None,
) -> dict:
    return {
        "name_contains": name,
        "rarity_contains": rarity,
        "pokedex": pokedex,
        "set_code": set_code,
        "set_name": set_name,
        "supertype": supertype,
        "rarity_tier": rarity_tier,
        "slot": slot,
        "tcg_card_id": tcg_card_id,
    }
