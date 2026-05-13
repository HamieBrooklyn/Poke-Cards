"""Gacha-style TCG card drops and collection."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Literal

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.cogs.catalog import CatalogBrowseView, _catalog_view_embed, _search_kwargs
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.catalog_search import (
    CATALOG_BROWSE_PAGE_CAP,
    any_catalog_content_filter_set,
    any_catalog_filter_set,
    search_catalog,
)
from poke_pon_bot.services.collection_search import any_filter_set, search_collection
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.config import Settings
from poke_pon_bot.context_reply import reply_target_user_id, resolve_collection_display_target
from poke_pon_bot.services.instance_public_id import compact_public_id_for_line, normalize_public_id
from poke_pon_bot.services.evolution import (
    EvolutionSuccess,
    quote_evolution,
    resolve_evolution_targets,
    run_collection_evolution,
)
from poke_pon_bot.services.pack_collage import render_pack_collage_png
from poke_pon_bot.services.wallet import WalletService, format_pokedollars
from poke_pon_bot.services.wishlist import (
    add_wishlist,
    is_wishlisted,
    remove_wishlist,
    wishlist_user_ids_for_cards,
)

_LOG = logging.getLogger(__name__)

# Public packs: anyone can claim one unrevealed slot until this many seconds elapse (matches View timeout).
_PACK_CLAIM_SECONDS = 180

_COLL_MAX_FETCH = 5000
_COLL_PAGE_CHAR_CAP = 1870
# Hard cap so a long inventory paginates into bite-sized pages (◀ ▶ flips).
_COLL_PAGE_LINE_CAP = 10

# Public binder (search, sort, detail) — linked from ``/colv`` / ``/coll`` replies.
COLLECTION_WEB_URL = "https://hamiebrooklyn.github.io/collection.html"


def _collection_web_footer() -> str:
    """Short line appended to collection list/flip replies so players can open the web binder."""
    return (
        f"\n\n_Browse and search your full binder (filters, card details):_\n"
        f"<{COLLECTION_WEB_URL}>"
    )


async def _notify_wishlisters(
    session_factory,
    interaction: discord.Interaction,
    *,
    card_id: int,
    card_name: str,
    obtainer_id: int,
) -> None:
    """Send a channel message tagging guild members who wishlisted the obtained card."""
    guild = interaction.guild
    if guild is None:
        return
    try:
        async with session_factory() as session:
            wl_map = await wishlist_user_ids_for_cards(
                session, [card_id], exclude_user_id=obtainer_id,
            )
        user_ids = wl_map.get(card_id, [])
        if not user_ids:
            return
        guild_member_ids = {m.id for m in guild.members}
        mentions = [f"<@{uid}>" for uid in user_ids if uid in guild_member_ids]
        if not mentions:
            return
        text = f"⭐ **{card_name}** was just obtained! Wishlisted by {', '.join(mentions)}"
        await interaction.followup.send(text)
    except Exception:
        _LOG.debug("wishlist notify failed for card_id=%s", card_id, exc_info=True)


def _hybrid_ephemeral(ctx: commands.Context) -> bool:
    """Keep replies **public** so slash and prefix behave the same in-channel."""
    return False


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _fmt_cd_sentence(seconds: float) -> str:
    s = int(round(max(1.0, seconds)))
    if s >= 120:
        return f"**{s // 60}** minutes"
    return f"**{s}** seconds"


def _drop_cooldown_message(retry_after: float, *, per_seconds: float) -> str:
    sec = max(1, int(round(retry_after)))
    mins, s = divmod(sec, 60)
    if mins and s:
        left = f"{mins}m {s}s"
    elif mins:
        left = f"{mins} min"
    else:
        left = f"{s}s"
    window = _fmt_cd_sentence(per_seconds)
    return (
        f"Pack drops are on a {window} cooldown for your account. "
        f"You can open another pack in **{left}**."
    )


class _DropBoostShopView(discord.ui.View):
    """Premium SKU button (checkout handled by Discord)."""

    def __init__(self, *, sku_id: int, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.add_item(discord.ui.Button(sku_id=sku_id))


def _fmt_obtained(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return discord.utils.format_dt(dt, "R")


def _card_id_copy_field_value(public_id: str) -> str:
    """Fenced block: on mobile, long-press the block → Copy (inline/footer IDs are awkward)."""
    return f"```\n{public_id}\n```"


def _add_card_id_copy_field(embed: discord.Embed, public_id: str) -> None:
    embed.add_field(
        name="Card ID — tap block to copy",
        value=_card_id_copy_field_value(public_id),
        inline=False,
    )


def _card_id_message_line(public_id: str) -> str:
    """Plain-text Card ID line emitted **outside** the embed.

    Mobile clients let you long-press inline backtick text (`like this`) to copy — embed bodies
    don't, so we surface the ID in `content=` for tap-to-copy.
    """
    return f"**Card ID:** `{public_id}`"


def _coll_one_line(rank: int, inst: UserCardInstance, card: Card) -> str:
    """Single compact row; Card ID in `` ` `` for tap-to-copy."""
    nm = _truncate(card.name, 22)
    sc = _truncate(card.set_code, 8)
    cn = _truncate(card.collector_number, 10)
    pid = compact_public_id_for_line(inst.public_id)
    return f"`{rank}.` **{nm}** `{sc}` #{cn} `{pid}`"


def _coll_pages(lines: list[str]) -> list[str]:
    """Split into Discord-sized plain-text pages (no blank lines between rows).

    Caps each page at ``_COLL_PAGE_LINE_CAP`` rows even when more would fit in
    ``_COLL_PAGE_CHAR_CAP`` characters — so a large inventory still flips in
    readable chunks instead of dumping hundreds of lines into one page.
    """
    if not lines:
        return []
    chunks: list[list[str]] = []
    buf: list[str] = []
    used = 0
    cap = _COLL_PAGE_CHAR_CAP
    for line in lines:
        extra = len(line) + (1 if buf else 0)
        if buf and (used + extra > cap or len(buf) >= _COLL_PAGE_LINE_CAP):
            chunks.append(buf)
            buf = []
            used = 0
            extra = len(line)
        buf.append(line)
        used += extra
    if buf:
        chunks.append(buf)
    total = len(chunks)
    pages: list[str] = []
    for i, chunk in enumerate(chunks):
        body = "\n".join(chunk)
        if total > 1:
            body = f"{i + 1}/{total}\n{body}"
        pages.append(body + _collection_web_footer())
    return pages


class CollectionListFlipView(discord.ui.View):
    """Plain-text collection pages with ◀ ▶."""

    def __init__(self, *, owner_id: int, viewer_id: int, pages: list[str]) -> None:
        if not pages:
            msg = "pages must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=600.0)
        self._owner_id = owner_id
        self._viewer_id = viewer_id
        self._pages = pages
        self._index = 0

        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
        self._prev.callback = self._on_prev
        self._next.callback = self._on_next
        self.add_item(self._prev)
        self.add_item(self._next)
        self._sync_nav()

    def _sync_nav(self) -> None:
        n = len(self._pages)
        if n <= 1:
            self._prev.disabled = True
            self._next.disabled = True
        else:
            self._prev.disabled = self._index <= 0
            self._next.disabled = self._index >= n - 1

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
        if interaction.user.id != self._viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this list can flip pages.",
                ephemeral=True,
            )
            return
        if self._index > 0:
            self._index -= 1
        await self._apply_page(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this list can flip pages.",
                ephemeral=True,
            )
            return
        if self._index < len(self._pages) - 1:
            self._index += 1
        await self._apply_page(interaction)

    async def _apply_page(self, interaction: discord.Interaction) -> None:
        self._sync_nav()
        await interaction.response.edit_message(
            content=self._pages[self._index],
            view=self,
        )


def _collection_view_embed(
    inst: UserCardInstance,
    card: Card,
    *,
    rank_note: str | None = None,
    collection_owner_id: int | None = None,
    viewer_id: int | None = None,
) -> discord.Embed:
    """Rich embed for one owned card (catalog + when you saved it)."""
    e = discord.Embed(title=card.name)
    header_bits: list[str] = []
    if (
        collection_owner_id is not None
        and viewer_id is not None
        and collection_owner_id != viewer_id
    ):
        header_bits.append(f"<@{collection_owner_id}> — viewing their collection")
    if rank_note:
        header_bits.append(rank_note)
    if header_bits:
        e.description = "\n\n".join(header_bits)
    e.set_image(url=card.image_large_url or card.image_small_url)

    e.add_field(name="Set", value=f"{card.set_name}\n`{card.set_code}`", inline=True)
    e.add_field(name="Card #", value=f"`{card.collector_number}`", inline=True)
    e.add_field(
        name="Printed rarity",
        value=card.tcg_rarity or "—",
        inline=True,
    )

    if card.dex_numbers:
        dex_txt = ", ".join(str(n) for n in card.dex_numbers)
        e.add_field(name="Pokédex #", value=dex_txt, inline=False)

    e.add_field(
        name="Saved to collection",
        value=_fmt_obtained(inst.obtained_at),
        inline=False,
    )
    if inst.evolution_stages and inst.evolution_stages > 0:
        e.add_field(
            name="Evolution tier (this copy)",
            value=str(inst.evolution_stages),
            inline=True,
        )

    # Card ID is intentionally **not** embedded — see _card_id_message_line. Callers attach it
    # via `content=` so mobile users can long-press to copy the value.
    e.set_footer(text=f"Catalog printing `{card.tcg_card_id}`")
    return e


async def _load_instance_and_card(
    session: AsyncSession,
    instance_id: int,
    owner_discord_id: int,
) -> tuple[UserCardInstance, Card] | None:
    inst = await session.get(UserCardInstance, instance_id)
    if inst is None or inst.discord_user_id != owner_discord_id:
        return None
    card = await session.get(Card, inst.card_id)
    if card is None:
        return None
    return (inst, card)


async def _load_instance_by_public_id(
    session: AsyncSession,
    public_id: str,
    owner_discord_id: int,
) -> tuple[UserCardInstance, Card] | None:
    n = normalize_public_id(public_id)
    if n is None:
        return None
    row = await session.execute(
        select(UserCardInstance, Card)
        .join(Card, UserCardInstance.card_id == Card.id)
        .where(
            UserCardInstance.discord_user_id == owner_discord_id,
            UserCardInstance.public_id == n,
        )
    )
    first = row.first()
    if first is None:
        return None
    return (first[0], first[1])


def _evolution_confirm_embed(
    inst: UserCardInstance,
    card: Card,
    target: Card,
    cost: int,
) -> discord.Embed:
    """Thumbnail = your current printing; large image = evolution target; cost in description."""
    e = discord.Embed(
        title="Confirm evolution",
        description=(
            f"**{card.name}** → **{target.name}**\n\n"
            f"**Cost:** **{format_pokedollars(cost)}**"
        ),
    )
    thumb = card.image_small_url or card.image_large_url
    if thumb:
        e.set_thumbnail(url=thumb)
    big = target.image_large_url or target.image_small_url
    if big:
        e.set_image(url=big)
    _add_card_id_copy_field(e, inst.public_id)
    e.set_footer(text=f"Will become catalog `{target.tcg_card_id}`")
    return e


class EvolutionConfirmView(discord.ui.View):
    """Preview current → evolved printing; Evolve commits, Cancel clears."""

    def __init__(
        self,
        gacha: GachaCog,
        owner_id: int,
        instance_id: int,
        *,
        before_name: str,
        target_name: str,
        target_card_id: int,
    ) -> None:
        super().__init__(timeout=300.0)
        self._gacha = gacha
        self._owner_id = owner_id
        self._instance_id = instance_id
        self._before_name = before_name
        self._target_name = target_name
        self._target_card_id = target_card_id

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None and msg.embeds:
            try:
                emb = msg.embeds[0].copy()
                emb.set_footer(text=(emb.footer.text or "") + " · Timed out")
                await msg.edit(embed=emb, view=self)
            except (discord.NotFound, discord.HTTPException):
                pass
        elif msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(label="Evolve", style=discord.ButtonStyle.success, row=0)
    async def evolve(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("This isn’t your evolution prompt.", ephemeral=True)
            return
        await interaction.response.defer(thinking=False)
        try:
            async with self._gacha.bot.async_session_factory() as session:
                res = await run_collection_evolution(
                    session,
                    self._gacha._wallet,
                    self._owner_id,
                    self._instance_id,
                    target_card_id=self._target_card_id,
                )
        except SQLAlchemyError:
            _LOG.exception("evolution confirm for user %s", self._owner_id)
            await interaction.followup.send(
                "Something went wrong while evolving. Try again.",
                ephemeral=True,
            )
            return
        if isinstance(res, str):
            for child in self.children:
                child.disabled = True
            emb = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
            if emb is not None:
                e2 = emb.copy()
                d = e2.description or ""
                e2.description = f"{d}\n\n⚠ {res}"
                await interaction.edit_original_response(embed=e2, view=self)
            else:
                await interaction.edit_original_response(content=res, embeds=[], view=self)
            return
        su: EvolutionSuccess = res
        line = (
            f"**Evolved!** {su.before_name} **→** {su.new_card.name} for **{format_pokedollars(su.cost)}** "
            f"— balance **{format_pokedollars(su.new_balance)}**."
        )
        done = _collection_view_embed(su.inst, su.new_card, rank_note=line)
        await interaction.edit_original_response(
            content=_card_id_message_line(su.inst.public_id),
            embed=done,
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("This isn’t your evolution prompt.", ephemeral=True)
            return
        emb = discord.Embed(
            title="Evolution cancelled",
            description=(
                f"You chose **Cancel** — **{self._before_name}** was not evolved "
                f"to **{self._target_name}**."
            ),
        )
        await interaction.response.edit_message(embed=emb, view=None)


_DISCORD_SELECT_MAX = 25


class _EvolutionBranchSelectMenu(discord.ui.Select):
    """String select for branch picking.

    discord.py 2.6+ chains ``Item._parent._run_checks``; some layouts leave ``_parent`` as a
    :class:`discord.ui.View`, which does not define ``_run_checks`` — skip the parent chain.
    """

    def __init__(self, evo_view: "EvolutionBranchView", options: list[discord.SelectOption]) -> None:
        super().__init__(placeholder="Choose evolution…", options=options, row=0)
        self._evo_view = evo_view

    async def _run_checks(self, interaction: discord.Interaction) -> bool:
        return await self.interaction_check(interaction)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._evo_view._pick_branch(interaction, int(self.values[0]))


class EvolutionBranchView(discord.ui.View):
    """When several evolutions exist in-set (e.g. Eevee), pick branch then confirm."""

    def __init__(
        self,
        gacha: GachaCog,
        owner_id: int,
        instance_id: int,
        inst: UserCardInstance,
        card: Card,
        targets: list[Card],
        rarity_class: RarityClass,
    ) -> None:
        super().__init__(timeout=300.0)
        self._gacha = gacha
        self._owner_id = owner_id
        self._instance_id = instance_id
        self._inst = inst
        self._card = card
        self._total_branch_count = len(targets)
        self._targets = targets[:_DISCORD_SELECT_MAX]
        self._rc = rarity_class
        self._had_more = self._total_branch_count > _DISCORD_SELECT_MAX
        self._selected_id: int | None = None

        options = [
            discord.SelectOption(
                label=_truncate(t.name, 100),
                value=str(t.id),
                description=_truncate(t.tcg_card_id, 100),
            )
            for t in self._targets
        ]
        self.add_item(_EvolutionBranchSelectMenu(self, options))

        ev = discord.ui.Button(label="Evolve", style=discord.ButtonStyle.success, row=1, disabled=True)
        ev.callback = self._on_evolve
        self._evolve_btn = ev
        self.add_item(ev)

        ca = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
        ca.callback = self._on_cancel
        self.add_item(ca)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None and msg.embeds:
            try:
                emb = msg.embeds[0].copy()
                emb.set_footer(text=(emb.footer.text or "") + " · Timed out")
                await msg.edit(embed=emb, view=self)
            except (discord.NotFound, discord.HTTPException):
                pass
        elif msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    async def _pick_branch(self, interaction: discord.Interaction, target_id: int) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("This isn’t your evolution prompt.", ephemeral=True)
            return
        self._selected_id = target_id
        self._evolve_btn.disabled = False
        target = next(t for t in self._targets if t.id == self._selected_id)
        async with self._gacha.bot.async_session_factory() as session:
            target_rc = await session.get(RarityClass, target.rarity_class_id)
        if target_rc is None:
            await interaction.response.send_message(
                "Rarity data is missing for that evolution.",
                ephemeral=True,
            )
            return
        q = quote_evolution(self._rc, self._inst.evolution_stages, target, target_rc)
        preview = _evolution_confirm_embed(self._inst, self._card, target, q.cost)
        base_foot = preview.footer.text or ""
        if self._had_more:
            extra = f"Showing first {_DISCORD_SELECT_MAX} of {self._total_branch_count} choices"
            preview.set_footer(text=f"{base_foot} · {extra}" if base_foot else extra)
        await interaction.response.edit_message(embed=preview, view=self)

    async def _on_evolve(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("This isn’t your evolution prompt.", ephemeral=True)
            return
        if self._selected_id is None:
            await interaction.response.send_message(
                "Pick an evolution from the menu first.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=False)
        try:
            async with self._gacha.bot.async_session_factory() as session:
                res = await run_collection_evolution(
                    session,
                    self._gacha._wallet,
                    self._owner_id,
                    self._instance_id,
                    target_card_id=self._selected_id,
                )
        except SQLAlchemyError:
            _LOG.exception("evolution branch confirm for user %s", self._owner_id)
            await interaction.followup.send(
                "Something went wrong while evolving. Try again.",
                ephemeral=True,
            )
            return
        if isinstance(res, str):
            for child in self.children:
                child.disabled = True
            emb = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
            if emb is not None:
                e2 = emb.copy()
                d = e2.description or ""
                e2.description = f"{d}\n\n⚠ {res}"
                await interaction.edit_original_response(embed=e2, view=self)
            else:
                await interaction.edit_original_response(content=res, embeds=[], view=self)
            return
        su: EvolutionSuccess = res
        line = (
            f"**Evolved!** {su.before_name} **→** {su.new_card.name} for **{format_pokedollars(su.cost)}** "
            f"— balance **{format_pokedollars(su.new_balance)}**."
        )
        done = _collection_view_embed(su.inst, su.new_card, rank_note=line)
        await interaction.edit_original_response(
            content=_card_id_message_line(su.inst.public_id),
            embed=done,
            view=None,
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("This isn’t your evolution prompt.", ephemeral=True)
            return
        emb = discord.Embed(
            title="Evolution cancelled",
            description=f"You cancelled evolving **{self._card.name}**.",
        )
        await interaction.response.edit_message(embed=emb, view=None)


class WishlistToggleButton(discord.ui.Button):
    """Star button that toggles the current card on/off the viewer's wishlist."""

    _STAR_ON = "⭐"
    _STAR_OFF = "☆"

    def __init__(self, *, session_factory, card_id: int, viewer_id: int, wishlisted: bool, row: int = 0) -> None:
        emoji = self._STAR_ON if wishlisted else self._STAR_OFF
        style = discord.ButtonStyle.primary if wishlisted else discord.ButtonStyle.secondary
        super().__init__(emoji=emoji, style=style, row=row)
        self._session_factory = session_factory
        self._card_id = card_id
        self._viewer_id = viewer_id
        self._wishlisted = wishlisted

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this view can wishlist.",
                ephemeral=True,
            )
            return
        try:
            async with self._session_factory() as session:
                if self._wishlisted:
                    await remove_wishlist(session, discord_user_id=self._viewer_id, card_id=self._card_id)
                    self._wishlisted = False
                else:
                    added = await add_wishlist(session, discord_user_id=self._viewer_id, card_id=self._card_id)
                    if not added:
                        await interaction.response.send_message(
                            "Wishlist is full (max 50 cards) or already wishlisted.",
                            ephemeral=True,
                        )
                        return
                    self._wishlisted = True
        except SQLAlchemyError:
            _LOG.exception("wishlist toggle card_id=%s user=%s", self._card_id, self._viewer_id)
            await interaction.response.send_message("Could not update wishlist. Try again.", ephemeral=True)
            return

        self.emoji = self._STAR_ON if self._wishlisted else self._STAR_OFF
        self.style = discord.ButtonStyle.primary if self._wishlisted else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self.view)

    def update(self, *, card_id: int, wishlisted: bool) -> None:
        """Refresh the button state (e.g. after flipping to a new card)."""
        self._card_id = card_id
        self._wishlisted = wishlisted
        self.emoji = self._STAR_ON if wishlisted else self._STAR_OFF
        self.style = discord.ButtonStyle.primary if wishlisted else discord.ButtonStyle.secondary


class SingleCardWishlistView(discord.ui.View):
    """Lightweight view with just a ⭐ wishlist toggle for a single card display."""

    def __init__(self, *, session_factory, card_id: int, viewer_id: int, wishlisted: bool) -> None:
        super().__init__(timeout=600.0)
        self.add_item(WishlistToggleButton(
            session_factory=session_factory,
            card_id=card_id,
            viewer_id=viewer_id,
            wishlisted=wishlisted,
            row=0,
        ))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


class CollectionFlipView(discord.ui.View):
    """Browse owned cards with ◀ ▶ and ⭐ wishlist toggle."""

    def __init__(
        self,
        *,
        session_factory,
        owner_id: int,
        viewer_id: int,
        instance_ids: list[int],
        first_card_id: int = 0,
        first_wishlisted: bool = False,
    ) -> None:
        if not instance_ids:
            msg = "instance_ids must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=600.0)
        self._session_factory = session_factory
        self._owner_id = owner_id
        self._viewer_id = viewer_id
        self._instance_ids = instance_ids
        self._index = 0

        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
        self._prev.callback = self._on_prev
        self._next.callback = self._on_next
        self.add_item(self._prev)
        self.add_item(self._next)

        self._wish_btn = WishlistToggleButton(
            session_factory=session_factory,
            card_id=first_card_id,
            viewer_id=viewer_id,
            wishlisted=first_wishlisted,
            row=0,
        )
        self.add_item(self._wish_btn)

        self._sync_nav_buttons()

    def _sync_nav_buttons(self) -> None:
        n = len(self._instance_ids)
        if n <= 1:
            self._prev.disabled = True
            self._next.disabled = True
        else:
            self._prev.disabled = self._index <= 0
            self._next.disabled = self._index >= n - 1

    @staticmethod
    def _rank_note(index: int, total: int) -> str:
        return f"**{index + 1}** / **{total}**"

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
        if interaction.user.id != self._viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this browser can flip pages.",
                ephemeral=True,
            )
            return
        if self._index > 0:
            self._index -= 1
        await self._update_message(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this browser can flip pages.",
                ephemeral=True,
            )
            return
        if self._index < len(self._instance_ids) - 1:
            self._index += 1
        await self._update_message(interaction)

    async def _update_message(self, interaction: discord.Interaction) -> None:
        iid = self._instance_ids[self._index]
        try:
            async with self._session_factory() as session:
                row = await _load_instance_and_card(session, iid, self._owner_id)
                if row is None:
                    await interaction.response.send_message(
                        "That card is no longer in your collection.",
                        ephemeral=True,
                    )
                    return
                inst, card = row
                embed = _collection_view_embed(
                    inst,
                    card,
                    rank_note=self._rank_note(self._index, len(self._instance_ids)),
                    collection_owner_id=self._owner_id,
                    viewer_id=self._viewer_id,
                )
                content = _card_id_message_line(inst.public_id)
                wishlisted = await is_wishlisted(
                    session, discord_user_id=self._viewer_id, card_id=card.id,
                )
        except SQLAlchemyError:
            _LOG.exception("collection flip navigation for instance %s", iid)
            await interaction.response.send_message(
                "Could not load that card. Try again.",
                ephemeral=True,
            )
            return

        self._wish_btn.update(card_id=card.id, wishlisted=wishlisted)
        self._sync_nav_buttons()
        await interaction.response.edit_message(content=content, embed=embed, view=self)


class ClaimSlotButton(discord.ui.Button):
    """Claim one revealed slot — first come per slot; each user may take at most one card."""

    def __init__(self, *, pick_view: "PackPickView", idx: int, card_name: str, row: int) -> None:
        label = _truncate(f"#{idx + 1} · Take · {card_name}", 80)
        super().__init__(style=discord.ButtonStyle.secondary, label=label, row=row)
        # Do not set Item._parent — discord.py chains _run_checks to _parent (nested Items only).
        self._pick_view = pick_view
        self._idx = idx

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._pick_view.try_claim(interaction, self._idx, self)


class PackPickView(discord.ui.View):
    """Public pack fight: anyone can grab one unrevealed slot until the countdown expires."""

    def __init__(
        self,
        *,
        session_factory,
        issuer_id: int,
        opener_mention: str,
        cards: list[Card],
        deadline_unix: int,
        private_pack: bool,
    ) -> None:
        super().__init__(timeout=float(_PACK_CLAIM_SECONDS))
        self._session_factory = session_factory
        self._issuer_id = issuer_id
        self._opener_mention = opener_mention
        self._cards = cards
        self._deadline_unix = deadline_unix
        self._private_pack = private_pack
        self._lock = asyncio.Lock()
        # slot_index -> claimer user id
        self._slot_claimer: dict[int, int] = {}
        # slot_index -> per-claim Card ID (public_id) so we can echo it in plain text
        # outside the embed (mobile long-press copy).
        self._slot_pid: dict[int, str] = {}
        self._finished = False

        for i, card in enumerate(cards):
            row = i // 5
            self.add_item(ClaimSlotButton(pick_view=self, idx=i, card_name=card.name, row=row))

    def build_content(self) -> str:
        expiry = f"**Expires** <t:{self._deadline_unix}:R>"
        if self._private_pack:
            base = (
                f"{self._opener_mention} opened a pack (**private** — only you see this).\n"
                f"{expiry}\n"
                "Tap **Take** on one slot to save it."
            )
        else:
            base = (
                f"{self._opener_mention} dropped cards, it's up for grabs!\n"
                f"{expiry}"
            )
        if not self._slot_pid:
            return base
        # Plain text outside the embed so mobile clients can long-press the inline `code` to copy.
        claimed_lines = ["", "**Claimed Card IDs** (long-press to copy):"]
        for i in sorted(self._slot_pid):
            uid = self._slot_claimer.get(i)
            who = f"<@{uid}>" if uid is not None else "?"
            name = self._cards[i].name if 0 <= i < len(self._cards) else ""
            name_part = f" {name} ·" if name else ""
            claimed_lines.append(f"• #{i + 1} {who} ·{name_part} `{self._slot_pid[i]}`")
        return base + "\n" + "\n".join(claimed_lines)

    async def try_claim(self, interaction: discord.Interaction, idx: int, button: discord.ui.Button) -> None:
        async with self._lock:
            if self._finished:
                await interaction.response.send_message("This pack is already finished.", ephemeral=True)
                return
            now = int(time.time())
            if now >= self._deadline_unix:
                await interaction.response.send_message("This pack has expired.", ephemeral=True)
                return

            uid = interaction.user.id
            if interaction.user.bot:
                await interaction.response.send_message("Bots can’t claim cards.", ephemeral=True)
                return
            if uid in self._slot_claimer.values():
                await interaction.response.send_message(
                    "You already claimed **one** card from this pack.",
                    ephemeral=True,
                )
                return
            if idx in self._slot_claimer:
                await interaction.response.send_message("That slot was already taken.", ephemeral=True)
                return

            card = self._cards[idx]
            drop = DropService()
            try:
                async with self._session_factory() as session:
                    db_card = await session.get(Card, card.id)
                    if db_card is None:
                        await interaction.response.send_message(
                            "That card no longer exists in the catalog.",
                            ephemeral=True,
                        )
                        return
                    drop_result = await drop.claim_card(
                        session,
                        discord_user_id=uid,
                        card=db_card,
                        source="drop",
                    )
                    await session.commit()
            except SQLAlchemyError as exc:
                await interaction.response.send_message(
                    f"Could not save that card: {exc}",
                    ephemeral=True,
                )
                return

            self._slot_claimer[idx] = uid
            self._slot_pid[idx] = drop_result.public_id
            button.disabled = True
            button.style = discord.ButtonStyle.success
            button.label = _truncate(f"#{idx + 1} · Taken · {card.name}", 80)

            all_taken = len(self._slot_claimer) >= len(self._cards)
            if all_taken:
                self._finished = True
                for child in self.children:
                    child.disabled = True
                self.stop()
                extra = f"\n\n✅ **All {len(self._cards)} card(s) claimed.**"
                await interaction.response.edit_message(
                    content=self.build_content() + extra,
                    view=self,
                )
            else:
                await interaction.response.edit_message(
                    content=self.build_content(),
                    view=self,
                )

            await _notify_wishlisters(
                self._session_factory,
                interaction,
                card_id=card.id,
                card_name=card.name,
                obtainer_id=uid,
            )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        self._finished = True
        msg = getattr(self, "message", None)
        if msg is None:
            return
        extra = (
            "\n\n⏱ **Time's up** — unclaimed cards are gone.\n"
            f"_Claimed **{len(self._slot_claimer)}** / **{len(self._cards)}**._"
        )
        try:
            base = self.build_content()
            await msg.edit(content=base + extra, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass


_SCOPE = [
    app_commands.Choice(name="Global catalog (g)", value="g"),
    app_commands.Choice(name="My collection (c)", value="c"),
]


class GachaCog(commands.Cog):
    """Weighted drops; chat commands (`pcd`, `cs`, `cv`, `pcolv`, `cevolve`) plus slash equivalents."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wallet = WalletService()
        self._settings: Settings = bot.settings
        self._drop_last_ts: dict[int, float] = {}
        self._drop_boost_cache: dict[int, tuple[bool, float]] = {}

    def _invalidate_drop_boost_cache(self, user_id: int | None) -> None:
        if user_id is None:
            return
        self._drop_boost_cache.pop(user_id, None)

    @commands.Cog.listener()
    async def on_entitlement_create(self, entitlement: discord.Entitlement) -> None:
        sku = self._settings.discord_drop_boost_sku_id
        if sku is None or entitlement.sku_id != sku:
            return
        self._invalidate_drop_boost_cache(entitlement.user_id)

    @commands.Cog.listener()
    async def on_entitlement_update(self, entitlement: discord.Entitlement) -> None:
        sku = self._settings.discord_drop_boost_sku_id
        if sku is None or entitlement.sku_id != sku:
            return
        self._invalidate_drop_boost_cache(entitlement.user_id)

    @commands.Cog.listener()
    async def on_entitlement_delete(self, entitlement: discord.Entitlement) -> None:
        sku = self._settings.discord_drop_boost_sku_id
        if sku is None or entitlement.sku_id != sku:
            return
        self._invalidate_drop_boost_cache(entitlement.user_id)

    async def _user_has_drop_boost(self, ctx: commands.Context, user_id: int) -> bool:
        if user_id in self._settings.drop_boost_test_user_ids:
            return True
        sku = self._settings.discord_drop_boost_sku_id
        if sku is None:
            return False
        inter = ctx.interaction
        if inter is not None and sku in inter.entitlement_sku_ids:
            return True
        if self.bot.application_id is None:
            return False
        now = time.monotonic()
        cached = self._drop_boost_cache.get(user_id)
        if cached is not None:
            has, until = cached
            if until > now:
                return has
        has_ent = False
        try:
            async for ent in self.bot.entitlements(
                user=discord.Object(user_id),
                skus=[discord.Object(sku)],
                limit=5,
                exclude_ended=True,
                exclude_deleted=True,
            ):
                if ent.sku_id != sku or ent.consumed or ent.is_expired():
                    continue
                has_ent = True
                break
        except discord.HTTPException:
            _LOG.exception("entitlement list failed (drop boost) user=%s", user_id)
            has_ent = False
        ttl = max(5.0, float(self._settings.drop_boost_entitlement_cache_ttl))
        self._drop_boost_cache[user_id] = (has_ent, now + ttl)
        return has_ent

    async def _effective_drop_cooldown_seconds(self, ctx: commands.Context, user_id: int) -> float:
        if await self._user_has_drop_boost(ctx, user_id):
            return float(self._settings.drop_cooldown_premium_seconds)
        return float(self._settings.drop_cooldown_base_seconds)

    async def _collection_view_execute(
        self,
        ctx: commands.Context,
        *,
        owner_id: int,
        viewer_id: int,
        slot: int | None,
        name: str | None,
        rarity: str | None,
        pokedex: int | None,
        card_ref: str | None = None,
    ) -> None:
        """`cv c` after defer — optional **card_ref** = **Card ID** for ``owner_id``'s collection."""
        ephe = _hybrid_ephemeral(ctx)
        peer = owner_id != viewer_id
        subj = f"<@{owner_id}>" if peer else "You"
        poss = "their" if peer else "your"

        if card_ref and card_ref.strip():
            if normalize_public_id(card_ref) is None:
                await ctx.send(
                    "That doesn’t look like a **Card ID** (grey block: **16** characters, "
                    "or **32** hex for older cards).",
                    ephemeral=ephe,
                )
                return
            try:
                async with self.bot.async_session_factory() as session:
                    row = await _load_instance_by_public_id(
                        session,
                        card_ref,
                        owner_id,
                    )
                    if row is None:
                        await ctx.send(
                            f"{subj} doesn't have a copy with that **Card ID**."
                            if peer
                            else "You don't have a copy with that **Card ID**.",
                            ephemeral=ephe,
                        )
                        return
                    inst, card = row
                    embed = _collection_view_embed(
                        inst,
                        card,
                        rank_note="**cv c** by **card_ref** (Card ID).",
                        collection_owner_id=owner_id if peer else None,
                        viewer_id=viewer_id if peer else None,
                    )
                    content = _card_id_message_line(inst.public_id)
                    wishlisted = await is_wishlisted(
                        session, discord_user_id=viewer_id, card_id=card.id,
                    )
            except SQLAlchemyError:
                _LOG.exception("cv c card_ref owner %s viewer %s", owner_id, viewer_id)
                await ctx.send(
                    "Could not load that card. Try again.",
                    ephemeral=ephe,
                )
                return
            view = SingleCardWishlistView(
                session_factory=self.bot.async_session_factory,
                card_id=card.id,
                viewer_id=viewer_id,
                wishlisted=wishlisted,
            )
            await ctx.send(content=content, embed=embed, view=view, ephemeral=ephe)
            return

        if not any_filter_set(
            name_contains=name,
            rarity_contains=rarity,
            pokedex=pokedex,
            slot=slot,
            public_id=None,
        ):
            await ctx.send(
                f"Use **`slot`** (e.g. **1** for newest match), **`card_ref`** (**Card ID**), and/or "
                f"**`name`** / **`rarity`** / **`pokedex`** (searching **{poss}** collection).",
                ephemeral=ephe,
            )
            return

        async with self.bot.async_session_factory() as session:
            if slot is not None:
                rows, total = await search_collection(
                    session,
                    discord_user_id=owner_id,
                    name_contains=name,
                    rarity_contains=rarity,
                    pokedex=pokedex,
                    slot=slot,
                    page_limit=1,
                )
            else:
                rows, total = await search_collection(
                    session,
                    discord_user_id=owner_id,
                    name_contains=name,
                    rarity_contains=rarity,
                    pokedex=pokedex,
                    slot=None,
                    page_limit=2,
                )

        if total == 0:
            await ctx.send(
                f"No cards matched{' for ' + subj if peer else ''} — try different filters.",
                ephemeral=ephe,
            )
            return

        if slot is not None:
            if slot > total:
                await ctx.send(
                    (
                        f"{subj} only has **{total}** matching card(s); **`slot`** must be **1–{total}**."
                        if peer
                        else f"You only have **{total}** matching card(s); **`slot`** must be **1–{total}**."
                    ),
                    ephemeral=ephe,
                )
                return
            if not rows:
                await ctx.send(
                    "Could not load that slot.",
                    ephemeral=ephe,
                )
                return
            inst, card = rows[0]
            note = (
                f"Showing match **#{slot}** of **{total}** (newest first)."
                if total > 1
                else None
            )
            embed = _collection_view_embed(
                inst,
                card,
                rank_note=note,
                collection_owner_id=owner_id if peer else None,
                viewer_id=viewer_id if peer else None,
            )
            async with self.bot.async_session_factory() as s2:
                w = await is_wishlisted(s2, discord_user_id=viewer_id, card_id=card.id)
            view = SingleCardWishlistView(
                session_factory=self.bot.async_session_factory,
                card_id=card.id,
                viewer_id=viewer_id,
                wishlisted=w,
            )
            await ctx.send(
                content=_card_id_message_line(inst.public_id),
                embed=embed,
                view=view,
                ephemeral=ephe,
            )
            return

        if total > 1:
            await ctx.send(
                (
                    f"{subj} has **{total}** cards matching those filters. Add **`slot`** "
                    f"(**1**–**{total}**, **1** = newest) or **`card_ref`** / narrow filters. "
                    "Use **`/cs c`** or **`cs c`** (reply or same filters) to list."
                    if peer
                    else f"You have **{total}** cards matching those filters. Add **`slot`** "
                    f"(**1**–**{total}**, **1** = newest) or **`card_ref`** / narrow filters. "
                    "Use **`/cs c`** or in chat **`cs c`** to list with the same filters."
                ),
                ephemeral=ephe,
            )
            return

        if not rows:
            await ctx.send(
                "Could not load that card.",
                ephemeral=ephe,
            )
            return

        inst, card = rows[0]
        embed = _collection_view_embed(
            inst,
            card,
            rank_note=(
                "Only one card matched those filters."
                if peer
                else "Only one card matched your filters."
            ),
            collection_owner_id=owner_id if peer else None,
            viewer_id=viewer_id if peer else None,
        )
        async with self.bot.async_session_factory() as s2:
            w = await is_wishlisted(s2, discord_user_id=viewer_id, card_id=card.id)
        view = SingleCardWishlistView(
            session_factory=self.bot.async_session_factory,
            card_id=card.id,
            viewer_id=viewer_id,
            wishlisted=w,
        )
        await ctx.send(
            content=_card_id_message_line(inst.public_id),
            embed=embed,
            view=view,
            ephemeral=ephe,
        )

    @commands.hybrid_command(
        name="cs",
        description="Search: global (g) or your collection (c) — chat: cs",
    )
    @app_commands.choices(scope=_SCOPE)
    @app_commands.describe(
        scope="**g** = imported catalog; **c** = your saved cards",
        card_ref="**g** = exact catalog printing `sv1-112` · **c** = your **Card ID** (optional filter)",
        name="**g** / **c**: name contains (substring)",
        rarity="**g** / **c**: printed rarity (substring)",
        pokedex="**g** / **c**: National Pokédex # on the card",
        set_code="**g** only: set code substring (e.g. sv1)",
        set_name="**g** only: set name substring",
        supertype="**g** only: e.g. Pokémon, Trainer, Energy",
        rarity_tier="**g** only: drop tier (substring, e.g. ultra_rare)",
        slot="**c** only: **1** = newest among your matches; narrows the list to one row",
        limit="**g** / **c**: max rows in the list (for **c**, ignored when **slot** is set)",
    )
    async def cs(
        self,
        ctx: commands.Context,
        scope: str,
        card_ref: str | None = None,
        name: str | None = None,
        rarity: str | None = None,
        pokedex: int | None = None,
        set_code: str | None = None,
        set_name: str | None = None,
        supertype: str | None = None,
        rarity_tier: str | None = None,
        slot: app_commands.Range[int, 1, 10_000] | None = None,
        limit: app_commands.Range[int, 1, 25] = 15,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        if scope not in ("g", "c"):
            await ctx.send("**scope** must be **g** (global catalog) or **c** (your collection).", ephemeral=False)
            return
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        tcg = card_ref if scope == "g" and card_ref and card_ref.strip() else None
        pub = card_ref if scope == "c" and card_ref and card_ref.strip() else None
        if scope == "g":
            if not any_catalog_filter_set(
                name_contains=name,
                rarity_contains=rarity,
                pokedex=pokedex,
                set_code=set_code,
                set_name=set_name,
                supertype=supertype,
                rarity_tier=rarity_tier,
                slot=None,
                tcg_card_id=tcg,
            ):
                await ctx.send(
                    "For **catalog (g)**, add at least one of **name**, **rarity**, **pokedex**, **set_code**, "
                    "**set_name**, **supertype**, **rarity_tier**, or **card_ref** (catalog id). "
                    "Slash: **`/cs`**. In chat: e.g. **`cs g`** (same as **`/cs`**, scope g).",
                    ephemeral=ephe,
                )
                return
            skw = _search_kwargs(
                name, rarity, pokedex, set_code, set_name, supertype, rarity_tier, None, tcg_card_id=tcg
            )
            try:
                async with self.bot.async_session_factory() as session:
                    rows, total = await search_catalog(
                        session,
                        page_limit=int(limit),
                        **skw,
                    )
            except SQLAlchemyError:
                _LOG.exception("cs g")
                await ctx.send("Could not search the catalog. Try again later.", ephemeral=ephe)
                return
            if total == 0:
                await ctx.send("No cards matched those filters.", ephemeral=ephe)
                return
            if not rows:
                await ctx.send("No rows returned — try other filters.", ephemeral=ephe)
                return
            lines: list[str] = []
            for i, c in enumerate(rows, start=1):
                r = c.tcg_rarity or "?"
                lines.append(
                    f"`{i}.` **{c.name}** — {c.set_name} `#{c.collector_number}` · *{r}*",
                )
            rest = total - len(rows)
            extra = (
                f"\n_…**{rest}** more in the catalog (narrow with filters or use **`/cv g`** or **`cv g`** in chat to see art)._\n"
                if rest > 0
                else ""
            )
            header = f"**Catalog (g)** — **{len(rows)}** of **{total}** (internal id order) — use **`/cv g`** (or **`cv g`**) for art.\n"
            body = _truncate(header + "\n".join(lines) + extra, 2000)
            await ctx.send(body, ephemeral=ephe)
            return
        if not any_filter_set(
            name_contains=name,
            rarity_contains=rarity,
            pokedex=pokedex,
            slot=slot,
            public_id=pub,
        ):
            await ctx.send(
                "Add at least one of **`name`**, **`rarity`**, **`pokedex`**, **`slot`**, or **`card_ref`** (Card ID).",
                ephemeral=ephe,
            )
            return
        if pub and normalize_public_id(pub) is None:
            await ctx.send(
                "That doesn’t look like a **Card ID** (16 characters, or 32 hex for older cards).",
                ephemeral=ephe,
            )
            return
        try:
            async with self.bot.async_session_factory() as session:
                rows, total = await search_collection(
                    session,
                    discord_user_id=uid,
                    name_contains=name,
                    rarity_contains=rarity,
                    pokedex=pokedex,
                    slot=slot,
                    public_id=pub,
                    page_limit=int(limit),
                )
        except SQLAlchemyError:
            _LOG.exception("cs c for user %s", uid)
            await ctx.send("Could not search your collection. Try again.", ephemeral=ephe)
            return
        if total == 0:
            await ctx.send("No cards matched those filters.", ephemeral=ephe)
            return
        if slot is not None and int(slot) > total:
            await ctx.send(
                f"You only have **{total}** matching card(s); **slot** must be **1**–**{total}** (newest first).",
                ephemeral=ephe,
            )
            return
        if not rows:
            await ctx.send("No rows returned — try other filters.", ephemeral=ephe)
            return
        out_lines: list[str] = []
        for rank, (inst, card) in enumerate(rows, start=1):
            dex_bit = ""
            if card.dex_numbers:
                dex_bit = f" · dex {', '.join(str(d) for d in card.dex_numbers[:2])}"
                if len(card.dex_numbers) > 2:
                    dex_bit += "…"
            r0 = _truncate(card.tcg_rarity or "?", 12)
            nm = _truncate(card.name, 18)
            sn = _truncate(card.set_name, 14)
            cn = _truncate(card.collector_number, 8)
            pid = compact_public_id_for_line(inst.public_id)
            out_lines.append(
                f"`{rank}.` **{nm}** — {sn} #{cn} · "
                f"*{r0}*{dex_bit} · {_fmt_obtained(inst.obtained_at)} · `{pid}`",
            )
        header = (
            f"**Collection (c)** — **{len(rows)}** of **{total}** (newest first; use **slot** for one row)\n"
        )
        if pub is None and slot is None and total > len(rows):
            header += f"_…and **{total - len(rows)}** more — narrow filters or raise **limit**._\n"
        await ctx.send(_truncate(header + "\n".join(out_lines), 2000), ephemeral=ephe)

    @commands.hybrid_command(
        name="cv",
        description="Card view (g) or your copy (c) — chat: cv",
    )
    @app_commands.choices(scope=_SCOPE)
    @app_commands.describe(
        scope="**g** = catalog printing · **c** = your copy",
        card_ref="**g** = exact catalog id (`sv1-112`) · **c** = your **Card ID**",
        slot="**g** / **c**: 1 = first in match list (order differs by scope; see /help or chelp)",
        name="**g** / **c**",
        rarity="**g** / **c**",
        pokedex="**g** / **c**",
        set_code="**g** only",
        set_name="**g** only",
        supertype="**g** only",
        rarity_tier="**g** only",
    )
    async def cv(
        self,
        ctx: commands.Context,
        scope: str,
        card_ref: str | None = None,
        slot: app_commands.Range[int, 1, 10_000] | None = None,
        name: str | None = None,
        rarity: str | None = None,
        pokedex: int | None = None,
        set_code: str | None = None,
        set_name: str | None = None,
        supertype: str | None = None,
        rarity_tier: str | None = None,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        if scope not in ("g", "c"):
            await ctx.send("**scope** must be **g** or **c**.", ephemeral=False)
            return
        ephe = _hybrid_ephemeral(ctx)
        viewer_id = ctx.author.id
        reply_owner = await reply_target_user_id(self.bot, ctx)
        owner_id = reply_owner if reply_owner is not None else viewer_id
        if scope == "c":
            await self._collection_view_execute(
                ctx,
                owner_id=owner_id,
                viewer_id=viewer_id,
                slot=slot,
                name=name,
                rarity=rarity,
                pokedex=pokedex,
                card_ref=card_ref,
            )
            return
        tcg = card_ref.strip() if card_ref and card_ref.strip() else None
        if not any_catalog_content_filter_set(
            name_contains=name,
            rarity_contains=rarity,
            pokedex=pokedex,
            set_code=set_code,
            set_name=set_name,
            supertype=supertype,
            rarity_tier=rarity_tier,
            tcg_card_id=tcg,
        ):
            await ctx.send(
                "For **catalog view (g)**, set at least one of **name**, **rarity**, **pokedex**, set filters, **rarity_tier**, or "
                "**`card_ref`**, optionally with **slot** — slash **`/cv`**, in chat type **`cv`**.",
                ephemeral=ephe,
            )
            return
        skw = _search_kwargs(
            name, rarity, pokedex, set_code, set_name, supertype, rarity_tier, None, tcg_card_id=tcg
        )
        try:
            async with self.bot.async_session_factory() as session:
                window_rows, total = await search_catalog(
                    session,
                    page_limit=CATALOG_BROWSE_PAGE_CAP,
                    **skw,
                )
        except SQLAlchemyError:
            _LOG.exception("cv g browse list")
            await ctx.send("Could not load the catalog. Try again later.", ephemeral=ephe)
            return
        if total == 0:
            await ctx.send("No cards matched those filters.", ephemeral=ephe)
            return
        if slot is not None and int(slot) > total:
            await ctx.send(
                f"Only **{total}** card(s) match; **slot** must be between **1** and **{total}**.",
                ephemeral=ephe,
            )
            return
        if slot is not None and int(slot) > len(window_rows) and int(slot) <= total:
            try:
                async with self.bot.async_session_factory() as session:
                    one_rows, t2 = await search_catalog(
                        session,
                        page_limit=1,
                        slot=int(slot),
                        **skw,
                    )
            except SQLAlchemyError:
                _LOG.exception("cv g single beyond window")
                await ctx.send("Could not load that card. Try again later.", ephemeral=ephe)
                return
            if not one_rows:
                await ctx.send("Could not load that card.", ephemeral=ephe)
                return
            card0 = one_rows[0]
            note = (
                f"Catalog **#{slot}** of **{t2}** — **◀▶** only show the first **{CATALOG_BROWSE_PAGE_CAP}**; "
                "narrow filters or use **slot** for any rank."
            )
            await ctx.send(embed=_catalog_view_embed(card0, rank_note=note), ephemeral=ephe)
            return
        start_idx = 0 if slot is None else int(slot) - 1
        if not (0 <= start_idx < len(window_rows)):
            await ctx.send("Invalid **slot** for this result set.", ephemeral=ephe)
            return
        card_ids = [c.id for c in window_rows]
        show_card = window_rows[start_idx]
        psize = len(card_ids)
        view = CatalogBrowseView(
            session_factory=self.bot.async_session_factory,
            owner_id=ctx.author.id,
            card_ids=card_ids,
            search_total=total,
        )
        view.set_index(start_idx)
        note = CatalogBrowseView._note(view._index, psize, total)
        await ctx.send(
            embed=_catalog_view_embed(show_card, rank_note=note),
            view=view,
            ephemeral=ephe,
        )

    @commands.hybrid_command(
        name="colv",
        aliases=["pcolv"],
        description="Flip through your collection (◀▶) — chat: pcolv (or colv)",
    )
    @app_commands.describe(
        member="Whose collection to browse — omit for yours",
        limit="Cap cards to flip through, newest first (omit to flip through the whole collection)",
    )
    async def collection_flip(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        # Omitting ``limit`` flips through the entire collection, newest → first claimed.
        # Range cap is a sanity guard for the rare user who actually types a number — the
        # uncapped path goes through `limit is None`.
        limit: app_commands.Range[int, 1, 1000] | None = None,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        viewer_id = ctx.author.id
        target = await resolve_collection_display_target(self.bot, ctx, member_param=member)
        if target.bot:
            await ctx.send("Bots don’t have collections.", ephemeral=ephe)
            return
        owner_id = int(target.id)
        lim = int(limit) if limit is not None else None
        try:
            async with self.bot.async_session_factory() as session:
                stmt = (
                    select(UserCardInstance.id)
                    .where(UserCardInstance.discord_user_id == owner_id)
                    .order_by(UserCardInstance.obtained_at.desc())
                )
                if lim is not None:
                    stmt = stmt.limit(lim)
                res = await session.execute(stmt)
                ids = [int(r[0]) for r in res.all()]
                if not ids:
                    if owner_id != viewer_id:
                        await ctx.send(f"<@{owner_id}> has an empty collection. **`pcd`**", ephemeral=ephe)
                    else:
                        await ctx.send("You have an empty collection. **`pcd`**", ephemeral=ephe)
                    return
                row0 = await _load_instance_and_card(session, ids[0], owner_id)
                if row0 is None:
                    await ctx.send("Could not load that collection. Try again.", ephemeral=ephe)
                    return
                inst0, card0 = row0
                embed = _collection_view_embed(
                    inst0,
                    card0,
                    rank_note=CollectionFlipView._rank_note(0, len(ids)),
                    collection_owner_id=owner_id,
                    viewer_id=viewer_id,
                )
                first_pid = inst0.public_id
                first_wishlisted = await is_wishlisted(
                    session, discord_user_id=viewer_id, card_id=card0.id,
                )
        except SQLAlchemyError:
            _LOG.exception("colv for owner %s viewer %s", owner_id, viewer_id)
            await ctx.send("Could not load that collection. Try again.", ephemeral=ephe)
            return

        view = CollectionFlipView(
            session_factory=self.bot.async_session_factory,
            owner_id=owner_id,
            viewer_id=viewer_id,
            instance_ids=ids,
            first_card_id=card0.id,
            first_wishlisted=first_wishlisted,
        )
        await ctx.send(
            content=_card_id_message_line(first_pid) + _collection_web_footer(),
            embed=embed,
            view=view,
            ephemeral=ephe,
        )

    @commands.hybrid_command(
        name="coll",
        aliases=["pcoll"],
        description="Collection text list (◀▶) — chat: pcoll (or coll)",
    )
    @app_commands.describe(
        member="Whose collection to list — omit for yours",
        limit="Max cards (1–5000)",
    )
    async def collection_list(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        limit: app_commands.Range[int, 1, 5000] = 500,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        viewer_id = ctx.author.id
        target = await resolve_collection_display_target(self.bot, ctx, member_param=member)
        if target.bot:
            await ctx.send("Bots don’t have collections.", ephemeral=ephe)
            return
        owner_id = int(target.id)
        lim = min(int(limit), _COLL_MAX_FETCH)
        try:
            async with self.bot.async_session_factory() as session:
                stmt = (
                    select(UserCardInstance, Card)
                    .join(Card, UserCardInstance.card_id == Card.id)
                    .where(UserCardInstance.discord_user_id == owner_id)
                    .order_by(UserCardInstance.obtained_at.desc())
                    .limit(lim)
                )
                res = await session.execute(stmt)
                pairs = res.all()
        except SQLAlchemyError:
            _LOG.exception("coll for owner %s viewer %s", owner_id, viewer_id)
            await ctx.send("Could not load that collection. Try again.", ephemeral=ephe)
            return

        if not pairs:
            if owner_id != viewer_id:
                await ctx.send(f"<@{owner_id}> has an empty collection. **`pcd`**", ephemeral=ephe)
            else:
                await ctx.send("You have an empty collection. **`pcd`**", ephemeral=ephe)
            return

        header = ""
        if owner_id != viewer_id:
            header = f"<@{owner_id}> — newest **{min(len(pairs), lim)}** saved cards\n\n"

        lines = [_coll_one_line(n, inst, card) for n, (inst, card) in enumerate(pairs, start=1)]
        pages = _coll_pages(lines)
        pages = [header + p for p in pages]
        view = CollectionListFlipView(owner_id=owner_id, viewer_id=viewer_id, pages=pages)
        await ctx.send(content=pages[0], view=view, ephemeral=ephe)

    @commands.hybrid_command(
        name="cevolve",
        description="Evolve a saved copy (by Card ID) — chat: cevolve",
    )
    @app_commands.describe(
        card_ref="Your **Card ID** for that copy (same as **`cv c`** **card_ref**)",
    )
    async def card_evolve(self, ctx: commands.Context, card_ref: str) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        ref = card_ref.strip() if card_ref else ""
        if not ref:
            await ctx.send(
                "Pass your **Card ID** — e.g. **`/cevolve`** with **card_ref**, or in chat **`cevolve`** "
                "then paste the id (same as **`cv c`**).",
                ephemeral=ephe,
            )
            return
        if normalize_public_id(ref) is None:
            await ctx.send(
                "That doesn’t look like a **Card ID** (grey block: **16** characters, "
                "or **32** hex for older cards).",
                ephemeral=ephe,
            )
            return
        try:
            async with self.bot.async_session_factory() as session:
                row = await _load_instance_by_public_id(session, ref, uid)
                if row is None:
                    await ctx.send(
                        "You don’t have a copy with that **Card ID**.",
                        ephemeral=ephe,
                    )
                    return
                inst, card = row
                targets = await resolve_evolution_targets(session, card)
                if not targets:
                    await ctx.send(
                        "This card can’t be evolved (no next stage in the catalog for this set).",
                        ephemeral=ephe,
                    )
                    return
                rc = await session.get(RarityClass, card.rarity_class_id)
                if rc is None:
                    await ctx.send("Rarity data is missing. Try again later.", ephemeral=ephe)
                    return
                if len(targets) == 1:
                    target = targets[0]
                    target_rc = await session.get(RarityClass, target.rarity_class_id)
                    if target_rc is None:
                        await ctx.send(
                            "Rarity data is missing for the evolution target. Try again later.",
                            ephemeral=ephe,
                        )
                        return
                    q = quote_evolution(rc, inst.evolution_stages, target, target_rc)
                    preview = _evolution_confirm_embed(inst, card, target, q.cost)
                    view = EvolutionConfirmView(
                        self,
                        uid,
                        inst.id,
                        before_name=card.name,
                        target_name=target.name,
                        target_card_id=target.id,
                    )
                else:
                    preview = discord.Embed(
                        title="Choose evolution",
                        description=(
                            f"**{card.name}** has **{len(targets)}** possible evolutions in this set.\n"
                            "Pick one from the menu, then tap **Evolve**."
                        ),
                    )
                    thumb = card.image_small_url or card.image_large_url
                    if thumb:
                        preview.set_thumbnail(url=thumb)
                    _add_card_id_copy_field(preview, inst.public_id)
                    if len(targets) > _DISCORD_SELECT_MAX:
                        preview.set_footer(
                            text=f"Menu shows the first {_DISCORD_SELECT_MAX} — sync covers full set catalog.",
                        )
                    view = EvolutionBranchView(self, uid, inst.id, inst, card, targets, rc)
        except SQLAlchemyError:
            _LOG.exception("cevolve preview for user %s", uid)
            await ctx.send("Something went wrong. Try again.", ephemeral=ephe)
            return

        await ctx.send(embed=preview, view=view, ephemeral=ephe)

    @commands.hybrid_command(
        name="drop_boost",
        description="Half drop cooldown — buy the durable SKU in Discord (optional)",
    )
    async def drop_boost_shop(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        sku = self._settings.discord_drop_boost_sku_id
        if sku is None:
            await ctx.send(
                "Drop boost isn’t configured (`DISCORD_DROP_BOOST_SKU_ID`).",
                ephemeral=True,
            )
            return
        base = self._settings.drop_cooldown_base_seconds
        prem = self._settings.drop_cooldown_premium_seconds
        try:
            await ctx.send(
                "**Half drop cooldown** — one-time purchase: after checkout, your **`/cd`** / **`pcd`** wait drops from "
                f"{_fmt_cd_sentence(base)} to {_fmt_cd_sentence(prem)}.\n"
                "Tap **Buy** below to open Discord’s purchase flow.",
                view=_DropBoostShopView(sku_id=sku),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            # 50035 + sku_id: common when SKU is draft / not published in Developer Portal.
            if exc.status == 400 and (
                exc.code == 50035
                or (exc.text and "sku" in exc.text.lower())
            ):
                _LOG.warning(
                    "drop_boost premium button rejected (sku likely unpublished): sku=%s %s",
                    sku,
                    exc.text,
                )
                help_msg = (
                    "Discord refused to show the **Buy** button: this SKU **does not exist or is unpublished** "
                    "for your app (see Developer Portal → Store / Monetization — publish the listing). "
                    "Confirm `DISCORD_DROP_BOOST_SKU_ID` matches an id from **`/dev list_skus`** with **purchasable** on."
                )
                if ctx.interaction is not None:
                    await ctx.interaction.followup.send(help_msg, ephemeral=True)
                else:
                    await ctx.send(help_msg)
                return
            raise

    @commands.hybrid_command(
        name="cd",
        aliases=["pcd"],
        description="Card drop (open a pack) — chat: pcd (or cd)",
    )
    @app_commands.describe(
        private="Pick this option (slash only) to make the pack visible to you only.",
    )
    async def card_drop(
        self,
        ctx: commands.Context,
        # Single-value Literal renders as a one-option choice in Discord — picking it flips on
        # ephemeral mode without the True/False follow-up that bools force.
        private: Literal["yes"] | None = None,
    ) -> None:
        is_private = private == "yes"
        uid = ctx.author.id
        per = await self._effective_drop_cooldown_seconds(ctx, uid)
        now = time.time()
        last = self._drop_last_ts.get(uid, 0.0)
        if last and now - last < per:
            msg = _drop_cooldown_message(per - (now - last), per_seconds=per)
            if ctx.interaction is not None and not ctx.interaction.response.is_done():
                await ctx.interaction.response.send_message(msg, ephemeral=_hybrid_ephemeral(ctx))
            else:
                await ctx.send(msg, ephemeral=_hybrid_ephemeral(ctx))
            return
        self._drop_last_ts[uid] = now

        if ctx.interaction:
            await ctx.defer(ephemeral=is_private)
        drop = DropService()
        async with self.bot.async_session_factory() as session:
            try:
                pack = await drop.roll_pack(session, drop_table_code="default")
            except RuntimeError as exc:
                await ctx.send(str(exc), ephemeral=False)
                return
            except LookupError as exc:
                await ctx.send(str(exc), ephemeral=False)
                return

        is_slash = ctx.interaction is not None
        private_reply = is_slash and is_private
        deadline_unix = int(time.time()) + _PACK_CLAIM_SECONDS
        view = PackPickView(
            session_factory=self.bot.async_session_factory,
            issuer_id=ctx.author.id,
            opener_mention=ctx.author.mention,
            cards=pack,
            deadline_unix=deadline_unix,
            private_pack=private_reply,
        )
        try:
            png = await render_pack_collage_png(pack)
        except (OSError, ValueError, httpx.HTTPError):
            png = None
        content = view.build_content()
        if png is not None:
            file = discord.File(png, filename="pack.png")
            msg = await ctx.send(
                content=content,
                file=file,
                ephemeral=private_reply,
                view=view,
            )
            view.message = msg
        else:
            # No collage — append a plain-text card list so the message still shows what dropped.
            fallback = "\n".join(
                f"**#{i + 1}** {c.name} — *{c.tcg_rarity or '?'}* · {c.set_name} #{c.collector_number}"
                for i, c in enumerate(pack)
            )
            msg = await ctx.send(
                content=content + "\n*(Could not build card collage.)*\n" + fallback,
                ephemeral=private_reply,
                view=view,
            )
            view.message = msg

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GachaCog(bot))
    _LOG.info(
        "Loaded gacha cog: `pcd`, `/drop_boost`, `/cd`, `/cs`, `/cv`, `/colv`, `/coll`, `/cevolve`."
    )

