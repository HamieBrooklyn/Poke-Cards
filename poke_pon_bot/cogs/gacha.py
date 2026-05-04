"""Gacha-style TCG card drops and collection."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

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
from poke_pon_bot.services.instance_public_id import compact_public_id_for_line, normalize_public_id
from poke_pon_bot.services.evolution import (
    EvolutionSuccess,
    quote_evolution,
    resolve_evolution_targets,
    run_collection_evolution,
)
from poke_pon_bot.services.pack_collage import render_pack_collage_png
from poke_pon_bot.services.wallet import WalletService, format_pokedollars

_LOG = logging.getLogger(__name__)

_COLL_MAX_FETCH = 5000
_COLL_PAGE_CHAR_CAP = 1870


def _hybrid_ephemeral(ctx: commands.Context) -> bool:
    """Keep replies **public** so slash and prefix behave the same in-channel."""
    return False


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


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


def _coll_one_line(rank: int, inst: UserCardInstance, card: Card) -> str:
    """Single compact row; Card ID in `` ` `` for tap-to-copy."""
    nm = _truncate(card.name, 22)
    sc = _truncate(card.set_code, 8)
    cn = _truncate(card.collector_number, 10)
    pid = compact_public_id_for_line(inst.public_id)
    return f"`{rank}.` **{nm}** `{sc}` #{cn} `{pid}`"


def _coll_pages(lines: list[str]) -> list[str]:
    """Split into Discord-sized plain-text pages (no blank lines between rows)."""
    if not lines:
        return []
    chunks: list[list[str]] = []
    buf: list[str] = []
    used = 0
    cap = _COLL_PAGE_CHAR_CAP
    for line in lines:
        extra = len(line) + (1 if buf else 0)
        if buf and used + extra > cap:
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
        pages.append(body)
    return pages


class CollectionListFlipView(discord.ui.View):
    """Plain-text collection pages with ◀ ▶."""

    def __init__(self, *, owner_id: int, pages: list[str]) -> None:
        if not pages:
            msg = "pages must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=600.0)
        self._owner_id = owner_id
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
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("Not your list.", ephemeral=True)
            return
        if self._index > 0:
            self._index -= 1
        await self._apply_page(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("Not your list.", ephemeral=True)
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
) -> discord.Embed:
    """Rich embed for one owned card (catalog + when you saved it)."""
    e = discord.Embed(title=card.name)
    if rank_note:
        e.description = rank_note
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

    _add_card_id_copy_field(e, inst.public_id)
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
        await interaction.edit_original_response(embed=done, view=None)

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
        q = quote_evolution(self._card, self._rc, self._inst.evolution_stages, target)
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
        await interaction.edit_original_response(embed=done, view=None)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("This isn’t your evolution prompt.", ephemeral=True)
            return
        emb = discord.Embed(
            title="Evolution cancelled",
            description=f"You cancelled evolving **{self._card.name}**.",
        )
        await interaction.response.edit_message(embed=emb, view=None)


class CollectionFlipView(discord.ui.View):
    """Browse owned cards with ◀ ▶."""

    def __init__(
        self,
        *,
        session_factory,
        owner_id: int,
        instance_ids: list[int],
    ) -> None:
        if not instance_ids:
            msg = "instance_ids must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=600.0)
        self._session_factory = session_factory
        self._owner_id = owner_id
        self._instance_ids = instance_ids
        self._index = 0

        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
        self._prev.callback = self._on_prev
        self._next.callback = self._on_next
        self.add_item(self._prev)
        self.add_item(self._next)
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
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This isn’t your collection browser.",
                ephemeral=True,
            )
            return
        if self._index > 0:
            self._index -= 1
        await self._update_message(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This isn’t your collection browser.",
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
                )
        except SQLAlchemyError:
            _LOG.exception("collection flip navigation for instance %s", iid)
            await interaction.response.send_message(
                "Could not load that card. Try again.",
                ephemeral=True,
            )
            return

        self._sync_nav_buttons()
        await interaction.response.edit_message(embed=embed, view=self)


class KeepCardButton(discord.ui.Button):
    """Pick one revealed slot — label shows slot index and Pokémon name."""

    def __init__(
        self,
        *,
        idx: int,
        card_id: int,
        card_name: str,
        issuer_id: int,
        session_factory,
        row: int,
    ) -> None:
        label = _truncate(f"#{idx + 1} · {card_name}", 80)
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            row=row,
        )
        self._idx = idx
        self._card_id = card_id
        self._issuer_id = issuer_id
        self._session_factory = session_factory

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._issuer_id:
            await interaction.response.send_message(
                "This pack isn’t yours to claim.",
                ephemeral=True,
            )
            return

        drop = DropService()
        title: str
        thumb: str
        footer_text: str

        try:
            async with self._session_factory() as session:
                card = await session.get(Card, self._card_id)
                if card is None:
                    await interaction.response.send_message(
                        "That card no longer exists in the catalog.",
                        ephemeral=True,
                    )
                    return

                title = card.name
                thumb = card.image_large_url or card.image_small_url
                footer_text = (
                    f"{card.set_name} · #{card.collector_number} · "
                    f"{card.tcg_rarity or 'Unknown rarity'}"
                )

                result = await drop.claim_card(
                    session,
                    discord_user_id=interaction.user.id,
                    card=card,
                    source="drop",
                )
                await session.commit()
        except SQLAlchemyError as exc:
            await interaction.response.send_message(
                f"Could not save that card: {exc}",
                ephemeral=True,
            )
            return

        kept = discord.Embed(title=f"Kept: {title}")
        kept.set_image(url=thumb)
        kept.set_footer(text=footer_text)
        _add_card_id_copy_field(kept, result.public_id)

        content = (
            f"{interaction.user.mention} chose **{title}** "
            f"(slot #{self._idx + 1}). The other revealed cards were not added."
        )

        await interaction.response.edit_message(
            content=content,
            embeds=[kept],
            attachments=[],
            view=None,
        )


class PackPickView(discord.ui.View):
    """Shown after a pack roll; user picks exactly one slot to keep."""

    def __init__(
        self,
        *,
        session_factory,
        issuer_id: int,
        cards: list[Card],
    ) -> None:
        super().__init__(timeout=600.0)
        for i, card in enumerate(cards):
            row = i // 5
            self.add_item(
                KeepCardButton(
                    idx=i,
                    card_id=card.id,
                    card_name=card.name,
                    issuer_id=issuer_id,
                    session_factory=session_factory,
                    row=row,
                )
            )


_SCOPE = [
    app_commands.Choice(name="Global catalog (g)", value="g"),
    app_commands.Choice(name="My collection (c)", value="c"),
]


class GachaCog(commands.Cog):
    """Weighted drops; chat prefix `c` (`cd`, `cs`, `cv`, `colv`, `cevolve`); slash equivalents."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wallet = WalletService()

    async def _collection_view_execute(
        self,
        ctx: commands.Context,
        *,
        slot: int | None,
        name: str | None,
        rarity: str | None,
        pokedex: int | None,
        card_ref: str | None = None,
    ) -> None:
        """`cv c` after defer — optional **card_ref** = your **Card ID**."""
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
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
                        uid,
                    )
                    if row is None:
                        await ctx.send(
                            "You don’t have a copy with that **Card ID**.",
                            ephemeral=ephe,
                        )
                        return
                    inst, card = row
                    embed = _collection_view_embed(
                        inst,
                        card,
                        rank_note="**cv c** by **card_ref** (Card ID).",
                    )
            except SQLAlchemyError:
                _LOG.exception("cv c card_ref for user %s", uid)
                await ctx.send(
                    "Could not load that card. Try again.",
                    ephemeral=ephe,
                )
                return
            await ctx.send(embed=embed, ephemeral=ephe)
            return

        if not any_filter_set(
            name_contains=name,
            rarity_contains=rarity,
            pokedex=pokedex,
            slot=slot,
            public_id=None,
        ):
            await ctx.send(
                "Use **`slot`** (e.g. **1** for your newest match), **`card_ref`** (your **Card ID**), and/or "
                "**`name`** / **`rarity`** / **`pokedex`**.",
                ephemeral=ephe,
            )
            return

        async with self.bot.async_session_factory() as session:
            if slot is not None:
                rows, total = await search_collection(
                    session,
                    discord_user_id=uid,
                    name_contains=name,
                    rarity_contains=rarity,
                    pokedex=pokedex,
                    slot=slot,
                    page_limit=1,
                )
            else:
                rows, total = await search_collection(
                    session,
                    discord_user_id=uid,
                    name_contains=name,
                    rarity_contains=rarity,
                    pokedex=pokedex,
                    slot=None,
                    page_limit=2,
                )

        if total == 0:
            await ctx.send(
                "No cards matched — try different filters.",
                ephemeral=ephe,
            )
            return

        if slot is not None:
            if slot > total:
                await ctx.send(
                    f"You only have **{total}** matching card(s); **`slot`** must be **1–{total}**.",
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
            embed = _collection_view_embed(inst, card, rank_note=note)
            await ctx.send(embed=embed, ephemeral=ephe)
            return

        if total > 1:
            await ctx.send(
                f"You have **{total}** cards matching those filters. Add **`slot`** "
                f"(**1**–**{total}**, **1** = newest) or **`card_ref`** / narrow filters. "
                "Use **`/cs c`** or in chat **`cs c`** (prefix **`c`**, then **`s`**, then scope **`c`**) to list with the same filters.",
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
            rank_note="Only one card matched your filters.",
        )
        await ctx.send(embed=embed, ephemeral=ephe)

    @commands.hybrid_command(
        name="cs",
        aliases=["s"],
        description="Search: global (g) or your collection (c) — in chat, type cs (prefix c + s)",
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
                    "Slash: **`/cs`**. In chat, prefix **`c`**: e.g. **`cs g`** (same as **`/cs`**, scope g).",
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
        aliases=["v"],
        description="Card view (g) or your copy (c) — in chat, type cv (prefix c + v)",
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
        if scope == "c":
            await self._collection_view_execute(
                ctx,
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
                "**`card_ref`**, optionally with **slot** — slash **`/cv`**, in chat type **`cv`** (prefix c).",
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
        aliases=["olv"],
        description="Flip through your collection (◀▶) — in chat, type colv (prefix c + olv)",
    )
    @app_commands.describe(
        limit="Max cards to include, newest first (1–50)",
    )
    async def collection_flip(
        self,
        ctx: commands.Context,
        limit: app_commands.Range[int, 1, 50] = 25,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        lim = int(limit)
        try:
            async with self.bot.async_session_factory() as session:
                res = await session.execute(
                    select(UserCardInstance.id)
                    .where(UserCardInstance.discord_user_id == uid)
                    .order_by(UserCardInstance.obtained_at.desc())
                    .limit(lim),
                )
                ids = [int(r[0]) for r in res.all()]
                if not ids:
                    await ctx.send("Empty collection. **`cd`**", ephemeral=ephe)
                    return
                row0 = await _load_instance_and_card(session, ids[0], uid)
                if row0 is None:
                    await ctx.send("Could not load your collection. Try again.", ephemeral=ephe)
                    return
                inst0, card0 = row0
                embed = _collection_view_embed(
                    inst0,
                    card0,
                    rank_note=CollectionFlipView._rank_note(0, len(ids)),
                )
        except SQLAlchemyError:
            _LOG.exception("colv for user %s", uid)
            await ctx.send("Could not load your collection. Try again.", ephemeral=ephe)
            return

        view = CollectionFlipView(
            session_factory=self.bot.async_session_factory,
            owner_id=uid,
            instance_ids=ids,
        )
        await ctx.send(embed=embed, view=view, ephemeral=ephe)

    @commands.hybrid_command(
        name="coll",
        aliases=["oll"],
        description="Collection text list (◀▶) — chat: coll",
    )
    @app_commands.describe(limit="Max cards (1–5000)")
    async def collection_list(
        self,
        ctx: commands.Context,
        limit: app_commands.Range[int, 1, 5000] = 500,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        lim = min(int(limit), _COLL_MAX_FETCH)
        try:
            async with self.bot.async_session_factory() as session:
                stmt = (
                    select(UserCardInstance, Card)
                    .join(Card, UserCardInstance.card_id == Card.id)
                    .where(UserCardInstance.discord_user_id == uid)
                    .order_by(UserCardInstance.obtained_at.desc())
                    .limit(lim)
                )
                res = await session.execute(stmt)
                pairs = res.all()
        except SQLAlchemyError:
            _LOG.exception("coll for user %s", uid)
            await ctx.send("Could not load your collection. Try again.", ephemeral=ephe)
            return

        if not pairs:
            await ctx.send("Empty collection. **`cd`**", ephemeral=ephe)
            return

        lines = [_coll_one_line(n, inst, card) for n, (inst, card) in enumerate(pairs, start=1)]
        pages = _coll_pages(lines)
        view = CollectionListFlipView(owner_id=uid, pages=pages)
        await ctx.send(content=pages[0], view=view, ephemeral=ephe)

    @commands.hybrid_command(
        name="cevolve",
        aliases=["evolve"],
        description="Evolve a saved copy (by Card ID) — in chat, type cevolve (prefix c + evolve)",
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
                    q = quote_evolution(card, rc, inst.evolution_stages, target)
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
        name="cd",
        aliases=["d"],
        description="Card drop (open a pack) — in chat, type cd (prefix c + d)",
    )
    @app_commands.describe(
        private="Only you see the pack (slash only; in chat, cd is a normal message)",
    )
    async def card_drop(
        self,
        ctx: commands.Context,
        private: bool = False,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=private)
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

        header = (
            "**Pack opened** — always **2** cards; extra slots may appear "
            "(rarer each time). Tap **one** button below to save that card."
        )

        slots_text = "\n".join(
            f"**#{i}** {c.name} — *{c.tcg_rarity or '?'}* · {c.set_name} #{c.collector_number}"
            for i, c in enumerate(pack, start=1)
        )
        embed = discord.Embed(
            title="Your reveals",
            description=_truncate(slots_text, 4096),
        )

        try:
            png = await render_pack_collage_png(pack)
        except (OSError, ValueError, httpx.HTTPError):
            png = None
        view = PackPickView(
            session_factory=self.bot.async_session_factory,
            issuer_id=ctx.author.id,
            cards=pack,
        )
        is_slash = ctx.interaction is not None
        private_reply = is_slash and private
        if png is not None:
            file = discord.File(png, filename="pack.png")
            embed.set_image(url="attachment://pack.png")
            await ctx.send(
                content=header,
                embed=embed,
                file=file,
                ephemeral=private_reply,
                view=view,
            )
        else:
            await ctx.send(
                content=header + "\n*(Could not build card collage.)*",
                embed=embed,
                ephemeral=private_reply,
                view=view,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GachaCog(bot))
    _LOG.info(
        "Loaded gacha cog: chat `cd`/`cs`/`cv`/… (prefix `c`), slash `/cd`, `/cs`, `/cv`, `/colv`, `/coll`, `/cevolve`."
    )

