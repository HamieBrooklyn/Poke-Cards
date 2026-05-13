"""Booster packs: `/packd`, `/packv`, `/packcolv`, `/packcat`, the open/scrap flip view, and SKU plumbing.

`/packd` buys a random pack for Pokedollars or the Discord SKU. `/packv` searches pack
series and buys the viewed pack via Crystals. `/packcat` shows the entire pack catalog as
a book-flip view of horizontal pack-art strips (4 per page by default, max 6) with
filter/sort controls (popular, rarest, price). Opening a pack eagerly saves all rolled
cards as :class:`UserCardInstance` rows, then shows a paginated flip view with a red **Scrap**
button — scrapping removes that copy from inventory; flipping past or letting the view time
out keeps it.
"""

from __future__ import annotations

import asyncio
import io
import logging

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.cogs.gacha import (
    _card_id_message_line,
    _collection_view_embed,
    _fmt_obtained,
    _hybrid_ephemeral,
    _truncate,
)
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_series import CardSeries
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.pack_instance import UserPackInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.crystals import InsufficientCrystalsError, format_crystals
from poke_pon_bot.services.discord_entitlement_admin import consume_entitlement
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.services.packs import (
    DEFAULT_RANDOM_PACK_CRYSTAL_PRICE,
    DEFAULT_RANDOM_PACK_PRICE,
    NoActiveSeriesError,
    PackAlreadyOpenedError,
    PackEmptyError,
    PackNotFoundError,
    PackService,
    UnknownSeriesError,
)
from poke_pon_bot.services.wallet import (
    InsufficientPokedollarsError,
    format_pokedollars,
)
from poke_pon_bot.services.wishlist import wishlist_user_ids_for_cards

_LOG = logging.getLogger(__name__)

_OPEN_VIEW_TIMEOUT = 600.0


async def _notify_pack_wishlisters(
    session_factory,
    interaction: discord.Interaction,
    *,
    pairs: list[tuple[UserCardInstance, Card]],
    obtainer_id: int,
) -> None:
    """After a pack is opened, tag guild members who wishlisted any of the obtained cards."""
    guild = interaction.guild
    if guild is None:
        return
    try:
        card_ids = list({card.id for _, card in pairs})
        async with session_factory() as session:
            wl_map = await wishlist_user_ids_for_cards(
                session, card_ids, exclude_user_id=obtainer_id,
            )
        if not wl_map:
            return
        guild_member_ids = {m.id for m in guild.members}
        card_name_map = {card.id: card.name for _, card in pairs}
        lines: list[str] = []
        for cid, user_ids in wl_map.items():
            mentions = [f"<@{uid}>" for uid in user_ids if uid in guild_member_ids]
            if mentions:
                name = card_name_map.get(cid, "Unknown")
                lines.append(f"⭐ **{name}** — wishlisted by {', '.join(mentions)}")
        if lines:
            await interaction.followup.send("\n".join(lines))
    except Exception:
        _LOG.debug("pack wishlist notify failed", exc_info=True)


def _pack_summary(pack: UserPackInstance, series: CardSeries) -> str:
    obtained = _fmt_obtained(pack.obtained_at)
    return f"**{series.display_name}** · obtained {obtained} · `{pack.public_id}`"


def _pack_view_embed(pack: UserPackInstance, series: CardSeries) -> discord.Embed:
    """Single-pack info card; the actual art is attached separately as ``pack.png``."""
    e = discord.Embed(title=f"{series.display_name} pack")
    desc = []
    if series.description:
        desc.append(series.description)
    desc.append(
        f"Cards per pack: **{series.cards_per_pack}** + **{series.code_cards_per_pack}** code card"
    )
    e.description = "\n".join(desc)
    e.add_field(name="Series code", value=f"`{series.code}`", inline=True)
    e.add_field(name="Status", value=("Opened" if pack.opened_at else "Unopened"), inline=True)
    e.add_field(name="Obtained", value=_fmt_obtained(pack.obtained_at), inline=True)
    if pack.opened_at:
        e.add_field(name="Opened", value=_fmt_obtained(pack.opened_at), inline=True)
    e.set_footer(text=f"Pack ID {pack.public_id}")
    return e


def _pack_id_message_line(pack: UserPackInstance) -> str:
    return f"**Pack ID:** `{pack.public_id}`"


def _render_placeholder_pack_image(series: CardSeries) -> Image.Image | None:
    """Draw a simple placeholder pack tile (returns a PIL Image, ``None`` on failure)."""
    try:
        w, h = 480, 680
        img = Image.new("RGBA", (w, h), (28, 32, 40, 255))
        draw = ImageDraw.Draw(img)
        try:
            title_font = ImageFont.truetype(
                "/System/Library/Fonts/Helvetica.ttc", 48
            )
            sub_font = ImageFont.truetype(
                "/System/Library/Fonts/Helvetica.ttc", 28
            )
        except OSError:
            title_font = ImageFont.load_default()
            sub_font = ImageFont.load_default()

        for y in range(h):
            shade = 28 + int(20 * (y / h))
            draw.line([(0, y), (w, y)], fill=(shade, shade + 4, shade + 12, 255))

        draw.rounded_rectangle((24, 24, w - 24, h - 24), radius=24, outline=(220, 220, 235, 220), width=4)
        draw.text((48, 60), "BOOSTER PACK", fill=(220, 230, 255, 255), font=sub_font)

        title = _truncate(series.display_name, 26)
        bbox = draw.textbbox((0, 0), title, font=title_font)
        tw = bbox[2] - bbox[0]
        draw.text(((w - tw) // 2, h // 2 - 80), title, fill=(255, 255, 255, 255), font=title_font)

        code_label = f"series · {series.code}"
        bbox = draw.textbbox((0, 0), code_label, font=sub_font)
        tw = bbox[2] - bbox[0]
        draw.text(((w - tw) // 2, h // 2 - 16), code_label, fill=(180, 200, 235, 255), font=sub_font)

        sets_line = _truncate(
            f"{series.cards_per_pack} cards + {series.code_cards_per_pack} code card",
            40,
        )
        bbox = draw.textbbox((0, 0), sets_line, font=sub_font)
        tw = bbox[2] - bbox[0]
        draw.text(((w - tw) // 2, h - 110), sets_line, fill=(220, 230, 240, 255), font=sub_font)

        return img
    except (OSError, ValueError):
        return None


def _render_placeholder_pack_art(series: CardSeries) -> io.BytesIO | None:
    """Generate a simple placeholder pack PNG for series without a curated art URL."""
    img = _render_placeholder_pack_image(series)
    if img is None:
        return None
    try:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf
    except (OSError, ValueError):
        return None


async def _resolve_pack_art_attachment(
    series: CardSeries,
) -> tuple[discord.File | None, str | None]:
    """Resolve pack art to a Discord attachment.

    Returns ``(file, attachment_url)`` — when a curated ``pack_art_url`` exists we just point
    the embed at it directly; otherwise we render a placeholder and ship it as a file.
    """
    if series.pack_art_url:
        return None, series.pack_art_url

    buf = _render_placeholder_pack_art(series)
    if buf is None:
        return None, None
    file = discord.File(buf, filename="pack.png")
    return file, "attachment://pack.png"


_CATALOG_TILE_HEIGHT = 560
# Cap each tile's width so wide logos (e.g. ``pokemontcg.io/<set>/logo.png`` which are
# often 5:1 rectangles) don't dominate the strip and squash neighbouring portrait packs.
# Slightly wider than the placeholder pack aspect (480:680 ≈ 0.71:1) so real pack art
# isn't cropped, but tight enough that a 6-tile row stays reasonable in Discord.
_CATALOG_TILE_MAX_WIDTH = 420
_CATALOG_TILE_GAP = 20
_CATALOG_LABEL_HEIGHT = 72
_CATALOG_BG = (24, 27, 33, 255)
_CATALOG_FETCH_TIMEOUT = 12.0


def _resize_pack_tile(im: Image.Image, target_h: int) -> Image.Image:
    """Scale pack art to ``target_h`` while keeping width within ``_CATALOG_TILE_MAX_WIDTH``.

    If the natural scaled width exceeds the cap, we letterbox horizontally onto a fixed-width
    canvas so wide assets (like horizontal set logos) don't visually dwarf portrait pack art.
    """
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    w, h = im.size
    if h == 0:
        return im
    new_w = max(1, int(w * target_h / h))
    if new_w <= _CATALOG_TILE_MAX_WIDTH:
        return im.resize((new_w, target_h), Image.Resampling.LANCZOS)

    # Fit within (max_width, target_h), letterbox onto a max-width canvas.
    fitted_h = max(1, int(h * _CATALOG_TILE_MAX_WIDTH / w))
    fitted = im.resize((_CATALOG_TILE_MAX_WIDTH, fitted_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (_CATALOG_TILE_MAX_WIDTH, target_h), _CATALOG_BG)
    canvas.paste(fitted, (0, max(0, (target_h - fitted_h) // 2)),
                 fitted if fitted.mode == "RGBA" else None)
    return canvas


async def _fetch_pack_art(
    client: httpx.AsyncClient, series: CardSeries
) -> Image.Image | None:
    if not series.pack_art_url:
        return None
    try:
        resp = await client.get(series.pack_art_url, follow_redirects=True)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).copy()
    except (httpx.HTTPError, OSError, ValueError):
        return None


async def _render_pack_catalog_strip_png(
    series_list: list[CardSeries],
    *,
    start_rank: int = 1,
) -> io.BytesIO | None:
    """Build a single PNG that lays the given pack series side-by-side, with rank labels.

    Each tile is resized to a common height; remote URLs are fetched in parallel and
    placeholder art is generated locally for series without ``pack_art_url``.
    """
    if not series_list:
        return None

    timeout = httpx.Timeout(_CATALOG_FETCH_TIMEOUT, connect=5.0)
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": "Poke-Cards-Bot/1.0"}
    ) as client:
        fetched = await asyncio.gather(
            *[_fetch_pack_art(client, s) for s in series_list],
            return_exceptions=False,
        )

    tiles: list[Image.Image] = []
    for series, art in zip(series_list, fetched, strict=True):
        tile: Image.Image | None
        if art is not None:
            tile = _resize_pack_tile(art, _CATALOG_TILE_HEIGHT)
        else:
            placeholder = _render_placeholder_pack_image(series)
            tile = _resize_pack_tile(placeholder, _CATALOG_TILE_HEIGHT) if placeholder else None
        if tile is None:
            tile = Image.new("RGBA", (260, _CATALOG_TILE_HEIGHT), (55, 60, 68, 255))
        tiles.append(tile)

    total_w = sum(t.width for t in tiles) + _CATALOG_TILE_GAP * max(0, len(tiles) - 1)
    total_h = _CATALOG_TILE_HEIGHT + _CATALOG_LABEL_HEIGHT
    canvas = Image.new("RGBA", (total_w, total_h), _CATALOG_BG)

    try:
        label_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
    except OSError:
        label_font = ImageFont.load_default()
    draw = ImageDraw.Draw(canvas)

    x = 0
    for i, tile in enumerate(tiles):
        if tile.mode == "RGBA":
            canvas.paste(tile, (x, 0), tile)
        else:
            canvas.paste(tile, (x, 0))

        label = f"#{start_rank + i}"
        bbox = draw.textbbox((0, 0), label, font=label_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        lx = x + max(0, (tile.width - tw) // 2)
        ly = _CATALOG_TILE_HEIGHT + max(0, (_CATALOG_LABEL_HEIGHT - th) // 2) - 4
        draw.text((lx, ly), label, fill=(235, 240, 255, 255), font=label_font)
        x += tile.width + _CATALOG_TILE_GAP

    try:
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf
    except (OSError, ValueError):
        return None


class _BuySeriesCrystalsButton(discord.ui.Button):
    """Buy the viewed pack series with Crystals."""

    def __init__(self, *, cog: "PacksCog", series: CardSeries, row: int = 0) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"Buy ({series.crystal_price} Crystals)",
            row=row,
        )
        self._cog = cog
        self._series_id = series.id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._cog._handle_buy_with_crystals(interaction, self._series_id)


class PackSeriesPurchaseView(discord.ui.View):
    """Pack-series view: buy this exact series with Crystals."""

    def __init__(
        self,
        *,
        cog: "PacksCog",
        owner_id: int,
        series: CardSeries,
    ) -> None:
        super().__init__(timeout=600.0)
        self._owner_id = owner_id
        self.add_item(_BuySeriesCrystalsButton(cog=cog, series=series))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This pack purchase prompt belongs to someone else — run `/packv` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


class PackSeriesFlipView(discord.ui.View):
    """`/packv` search results: one pack series visual per page."""

    def __init__(
        self,
        *,
        cog: "PacksCog",
        owner_id: int,
        viewer_id: int,
        series_ids: list[int],
        first_series: CardSeries,
    ) -> None:
        if not series_ids:
            msg = "series_ids must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=600.0)
        self._cog = cog
        self._owner_id = owner_id
        self._viewer_id = viewer_id
        self._ids = list(series_ids)
        self._index = 0

        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
        self._buy = discord.ui.Button(style=discord.ButtonStyle.primary, row=0)
        self._prev.callback = self._on_prev
        self._next.callback = self._on_next
        self._buy.callback = self._on_buy
        self.add_item(self._prev)
        self.add_item(self._next)
        self.add_item(self._buy)
        self._sync_nav(first_series)

    def _sync_nav(self, series: CardSeries | None = None) -> None:
        n = len(self._ids)
        self._prev.disabled = n <= 1 or self._index <= 0
        self._next.disabled = n <= 1 or self._index >= n - 1
        self._buy.disabled = series is None
        self._buy.label = (
            f"Buy ({series.crystal_price} Crystals)" if series is not None else "Buy"
        )

    @staticmethod
    def _rank_note(idx: int, total: int) -> str:
        return f"**{idx + 1}** / **{total}**"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this pack browser can use these controls.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if self._index > 0:
            self._index -= 1
        await self._render_view(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if self._index < len(self._ids) - 1:
            self._index += 1
        await self._render_view(interaction)

    async def _on_buy(self, interaction: discord.Interaction) -> None:
        await self._cog._handle_buy_with_crystals(interaction, self._ids[self._index])

    async def _render_view(self, interaction: discord.Interaction) -> None:
        series_id = self._ids[self._index]
        try:
            async with self._cog.bot.async_session_factory() as session:
                series = await session.get(CardSeries, series_id)
                if series is None or not series.is_active:
                    await interaction.response.send_message(
                        "That pack series is no longer available.",
                        ephemeral=True,
                    )
                    return
                file, art_url = await _resolve_pack_art_attachment(series)
        except SQLAlchemyError:
            _LOG.exception("packv flip failed for series_id %s", series_id)
            await interaction.response.send_message(
                "Could not load that pack. Try again.",
                ephemeral=True,
            )
            return

        embed = self._cog._series_embed(series)
        if art_url:
            embed.set_image(url=art_url)
        embed.description = (embed.description or "") + (
            f"\n\n{self._rank_note(self._index, len(self._ids))}"
        )
        self._sync_nav(series)
        kwargs: dict = {"embed": embed, "view": self}
        if file is not None:
            kwargs["attachments"] = [file]
        else:
            kwargs["attachments"] = []
        await interaction.response.edit_message(**kwargs)


class PackCatalogFlipView(discord.ui.View):
    """`/packcat` book-flip view: horizontal strip of pack art tiles per page."""

    def __init__(
        self,
        *,
        cog: "PacksCog",
        viewer_id: int,
        pages: list[list[CardSeries]],
        per_page: int,
        sort_label: str,
        scope_label: str,
        filter_label: str | None,
        opened_counts: dict[int, int],
        top_rarity: dict[int, int],
        rarity_names: dict[int, str],
        sort_key: str,
    ) -> None:
        if not pages:
            msg = "pages must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=600.0)
        self._cog = cog
        self._viewer_id = viewer_id
        self._pages = pages
        self._per_page = per_page
        self._sort_label = sort_label
        self._scope_label = scope_label
        self._filter_label = filter_label
        self._opened = opened_counts
        self._top_rarity = top_rarity
        self._rarity_names = rarity_names
        self._sort_key = sort_key
        self._index = 0

        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
        self._prev.callback = self._on_prev
        self._next.callback = self._on_next
        self.add_item(self._prev)
        self.add_item(self._next)
        self._sync_nav()

    @property
    def total_series(self) -> int:
        return sum(len(p) for p in self._pages)

    def _sync_nav(self) -> None:
        n = len(self._pages)
        self._prev.disabled = n <= 1 or self._index <= 0
        self._next.disabled = n <= 1 or self._index >= n - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._viewer_id:
            await interaction.response.send_message(
                "Only the person who ran `/packcat` can flip these pages.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if self._index > 0:
            self._index -= 1
        await self._render_page(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if self._index < len(self._pages) - 1:
            self._index += 1
        await self._render_page(interaction)

    def build_embed_for_initial(self) -> discord.Embed:
        return self._build_embed(start_rank=1)

    def _start_rank(self) -> int:
        return self._per_page * self._index + 1

    def _build_embed(self, *, start_rank: int) -> discord.Embed:
        page_series = self._pages[self._index]
        embed = discord.Embed(title="Pack catalog")

        header_bits = [f"sorted by **{self._sort_label}**{self._scope_label}"]
        header_bits.append(f"**{self.total_series}** series")
        if self._filter_label:
            header_bits.append(f"filter: `{self._filter_label}`")
        header_bits.append(f"page **{self._index + 1}/{len(self._pages)}**")

        lines: list[str] = []
        for i, s in enumerate(page_series, start=start_rank):
            row_bits = [
                f"**#{i}** · **{_truncate(s.display_name, 40)}** "
                f"`{s.code}` · **{s.crystal_price} 💎**",
            ]
            tier_id = self._top_rarity.get(s.id)
            tier_name = (
                self._rarity_names.get(int(tier_id)) if tier_id is not None else None
            )
            if self._sort_key == "rarest" and tier_name:
                row_bits.append(f"top: **{tier_name}**")
            elif tier_name and self._sort_key in {"default", "low_cost", "high_cost"}:
                row_bits.append(f"top: {tier_name}")
            opened_n = self._opened.get(s.id, 0)
            if self._sort_key == "popular":
                row_bits.append(f"**{opened_n}** opened")
            elif opened_n:
                row_bits.append(f"{opened_n} opened")
            lines.append(" · ".join(row_bits))

        embed.description = " · ".join(header_bits) + "\n\n" + "\n".join(lines)
        embed.set_image(url="attachment://packcat.png")
        embed.set_footer(text="Buy with /packv <series code>  •  /packd for a random pack")
        return embed

    async def render_page_attachment(self) -> discord.File | None:
        """Render the current page's horizontal pack-art strip as a fresh attachment."""
        page_series = self._pages[self._index]
        buf = await _render_pack_catalog_strip_png(
            page_series, start_rank=self._start_rank()
        )
        if buf is None:
            return None
        return discord.File(buf, filename="packcat.png")

    async def _render_page(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            file = await self.render_page_attachment()
        except (OSError, ValueError):
            _LOG.exception("packcat strip render failed on page %s", self._index)
            await interaction.followup.send(
                "Could not render that page. Try again.",
                ephemeral=True,
            )
            return

        self._sync_nav()
        embed = self._build_embed(start_rank=self._start_rank())
        attachments = [file] if file is not None else []
        await interaction.edit_original_response(
            embed=embed, attachments=attachments, view=self
        )


class _OpenPackButton(discord.ui.Button):
    def __init__(self, *, cog: "PacksCog", pack_instance_id: int) -> None:
        super().__init__(style=discord.ButtonStyle.success, label="Open", row=0)
        self._cog = cog
        self._pack_instance_id = pack_instance_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._cog._handle_open_pack(interaction, self._pack_instance_id)


class PackViewView(discord.ui.View):
    """Pack-card view (single pack) with an Open button (only for the owner)."""

    def __init__(self, *, cog: "PacksCog", pack: UserPackInstance, owner_id: int) -> None:
        super().__init__(timeout=600.0)
        self._owner_id = owner_id
        if pack.opened_at is None:
            self.add_item(_OpenPackButton(cog=cog, pack_instance_id=pack.id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "Only the pack's owner can use these controls.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


class PackCollectionFlipView(discord.ui.View):
    """`/packcolv` flip view over unopened packs; Open button acts on the current page."""

    def __init__(
        self,
        *,
        cog: "PacksCog",
        owner_id: int,
        viewer_id: int,
        pack_instance_ids: list[int],
    ) -> None:
        if not pack_instance_ids:
            msg = "pack_instance_ids must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=600.0)
        self._cog = cog
        self._owner_id = owner_id
        self._viewer_id = viewer_id
        self._ids = list(pack_instance_ids)
        self._index = 0

        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
        self._open = discord.ui.Button(label="Open", style=discord.ButtonStyle.success, row=0)
        self._prev.callback = self._on_prev
        self._next.callback = self._on_next
        self._open.callback = self._on_open
        self.add_item(self._prev)
        self.add_item(self._next)
        self.add_item(self._open)
        self._sync_nav()

    def _sync_nav(self) -> None:
        n = len(self._ids)
        self._prev.disabled = n <= 1 or self._index <= 0
        self._next.disabled = n <= 1 or self._index >= n - 1
        self._open.disabled = n == 0 or self._owner_id != self._viewer_id

    @staticmethod
    def _rank_note(idx: int, total: int) -> str:
        return f"**{idx + 1}** / **{total}**"

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
        await self._render_view(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this browser can flip pages.",
                ephemeral=True,
            )
            return
        if self._index < len(self._ids) - 1:
            self._index += 1
        await self._render_view(interaction)

    async def _on_open(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "Only the pack's owner can open it.",
                ephemeral=True,
            )
            return
        if not self._ids:
            await interaction.response.send_message(
                "No more unopened packs in this view.",
                ephemeral=True,
            )
            return
        await self._cog._handle_open_pack(interaction, self._ids[self._index])

    async def _render_view(self, interaction: discord.Interaction) -> None:
        pack_id = self._ids[self._index]
        try:
            async with self._cog.bot.async_session_factory() as session:
                pack = await session.get(UserPackInstance, pack_id)
                if pack is None or pack.discord_user_id != self._owner_id:
                    await interaction.response.send_message(
                        "That pack is no longer available.",
                        ephemeral=True,
                    )
                    return
                series = await session.get(CardSeries, pack.series_id)
                if series is None:
                    await interaction.response.send_message(
                        "Pack series not found.",
                        ephemeral=True,
                    )
                    return
                file, art_url = await _resolve_pack_art_attachment(series)
        except SQLAlchemyError:
            _LOG.exception("packcolv flip failed for pack_id %s", pack_id)
            await interaction.response.send_message(
                "Could not load that pack. Try again.",
                ephemeral=True,
            )
            return

        embed = _pack_view_embed(pack, series)
        if art_url:
            embed.set_image(url=art_url)
        embed.description = (embed.description or "") + (
            f"\n\n{self._rank_note(self._index, len(self._ids))}"
        )

        self._sync_nav()
        kwargs: dict = {
            "content": _pack_id_message_line(pack),
            "embed": embed,
            "view": self,
        }
        if file is not None:
            kwargs["attachments"] = [file]
        await interaction.response.edit_message(**kwargs)

    async def remove_current_after_open(self) -> None:
        """Drop the just-opened pack from the carousel; called by the open handler."""
        if self._ids:
            del self._ids[self._index]
            if self._index >= len(self._ids) and self._ids:
                self._index = len(self._ids) - 1
        self._sync_nav()


class _BuyRandomPokedollarsButton(discord.ui.Button):
    """Buy a random active-series pack with Pokedollars from `/packd`."""

    def __init__(self, *, cog: "PacksCog") -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"Buy ({format_pokedollars(DEFAULT_RANDOM_PACK_PRICE)})",
            row=0,
        )
        self._cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._cog._handle_buy_random_with_pokedollars(interaction)


class _BuyRandomCrystalsButton(discord.ui.Button):
    """Buy a random active-series pack with Crystals from `/packd`."""

    def __init__(self, *, cog: "PacksCog") -> None:
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"Buy ({format_crystals(DEFAULT_RANDOM_PACK_CRYSTAL_PRICE)})",
            row=0,
        )
        self._cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._cog._handle_buy_random_with_crystals(interaction)


class PackDropPurchaseView(discord.ui.View):
    """`/packd` view: buy a random pack with Pokedollars, Crystals, or the configured SKU."""

    def __init__(
        self,
        *,
        cog: "PacksCog",
        owner_id: int,
        consumable_sku_id: int | None,
    ) -> None:
        super().__init__(timeout=600.0)
        self._owner_id = owner_id
        self.add_item(_BuyRandomPokedollarsButton(cog=cog))
        self.add_item(_BuyRandomCrystalsButton(cog=cog))
        if consumable_sku_id is not None:
            self.add_item(discord.ui.Button(sku_id=consumable_sku_id, row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This pack drop prompt belongs to someone else — run `/packd` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


class _ScrapButton(discord.ui.Button):
    def __init__(self, *, view: "OpenedPackFlipView") -> None:
        super().__init__(style=discord.ButtonStyle.danger, label="Scrap", row=0)
        self._opened_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._opened_view._on_scrap(interaction)


class OpenedPackFlipView(discord.ui.View):
    """Page through freshly opened cards; ◀ ▶ navigate, **Scrap** discards the current copy.

    Cards are saved to inventory **before** this view shows (eager save in
    :meth:`PackService.open_pack`), so closing or timing out simply keeps everything that
    wasn't scrapped.
    """

    def __init__(
        self,
        *,
        cog: "PacksCog",
        owner_id: int,
        instance_ids: list[int],
    ) -> None:
        if not instance_ids:
            msg = "instance_ids must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=_OPEN_VIEW_TIMEOUT)
        self._cog = cog
        self._owner_id = owner_id
        self._ids = list(instance_ids)
        self._index = 0

        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
        self._scrap = _ScrapButton(view=self)
        self._prev.callback = self._on_prev
        self._next.callback = self._on_next
        self.add_item(self._prev)
        self.add_item(self._next)
        self.add_item(self._scrap)
        self._sync_nav()

    def _sync_nav(self) -> None:
        n = len(self._ids)
        self._prev.disabled = n <= 1 or self._index <= 0
        self._next.disabled = n <= 1 or self._index >= n - 1
        self._scrap.disabled = n == 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "Only the pack opener can use these controls.",
                ephemeral=True,
            )
            return False
        return True

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
        if self._index > 0:
            self._index -= 1
        await self._render_view(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if self._index < len(self._ids) - 1:
            self._index += 1
        await self._render_view(interaction)

    async def _on_scrap(self, interaction: discord.Interaction) -> None:
        if not self._ids:
            await interaction.response.send_message(
                "Nothing left to scrap.", ephemeral=True
            )
            return
        scrap_id = self._ids[self._index]
        try:
            async with self._cog.bot.async_session_factory() as session:
                ps = PackService()
                await ps.scrap_card(session, instance_id=scrap_id, owner_id=self._owner_id)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("scrap failed for instance %s", scrap_id)
            await interaction.response.send_message(
                "Could not scrap that card. Try again.",
                ephemeral=True,
            )
            return

        del self._ids[self._index]
        if self._index >= len(self._ids) and self._ids:
            self._index = len(self._ids) - 1

        if not self._ids:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content="All cards processed — your kept pulls are in your collection.",
                embed=None,
                attachments=[],
                view=self,
            )
            return

        await self._render_view(interaction)

    async def _render_view(self, interaction: discord.Interaction) -> None:
        iid = self._ids[self._index]
        try:
            async with self._cog.bot.async_session_factory() as session:
                inst = await session.get(UserCardInstance, iid)
                if inst is None or inst.discord_user_id != self._owner_id:
                    await interaction.response.send_message(
                        "That card is no longer in your collection.",
                        ephemeral=True,
                    )
                    return
                card = await session.get(Card, inst.card_id)
                if card is None:
                    await interaction.response.send_message(
                        "Catalog row missing — please report this.",
                        ephemeral=True,
                    )
                    return
        except SQLAlchemyError:
            _LOG.exception("OpenedPackFlipView refresh failed for inst %s", iid)
            await interaction.response.send_message(
                "Could not load that card. Try again.",
                ephemeral=True,
            )
            return

        rank_note = f"**{self._index + 1}** / **{len(self._ids)}**"
        embed = _collection_view_embed(
            inst,
            card,
            rank_note=rank_note,
            collection_owner_id=self._owner_id,
            viewer_id=self._owner_id,
        )
        self._sync_nav()
        await interaction.response.edit_message(
            content=_card_id_message_line(inst.public_id),
            embed=embed,
            attachments=[],
            view=self,
        )


class PacksCog(commands.Cog):
    """Pack purchases, viewing, and the open/scrap flow."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _series_embed(self, series: CardSeries) -> discord.Embed:
        embed = discord.Embed(title=f"{series.display_name} pack")
        desc = []
        if series.description:
            desc.append(series.description)
        desc.append(
            f"Contains **{series.cards_per_pack} cards** + "
            f"**{series.code_cards_per_pack} code card**."
        )
        desc.append(f"Buy this exact pack for **{series.crystal_price} Crystals**.")
        embed.description = "\n".join(desc)
        embed.add_field(name="Series code", value=f"`{series.code}`", inline=True)
        embed.add_field(name="Crystal price", value=f"**{series.crystal_price}** 💎", inline=True)
        embed.set_footer(
            text=(
                f"Use /packd for a random pack: {format_pokedollars(DEFAULT_RANDOM_PACK_PRICE)} or "
                f"{format_crystals(DEFAULT_RANDOM_PACK_CRYSTAL_PRICE)}."
            ),
        )
        return embed

    # ------------------------------------------------------------------------------- handlers

    async def _handle_buy_with_crystals(
        self, interaction: discord.Interaction, series_id: int
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        ps = PackService()
        try:
            async with self.bot.async_session_factory() as session:
                pack = await ps.purchase_with_crystals(
                    session,
                    discord_user_id=interaction.user.id,
                    series_id=series_id,
                    guild_id=interaction.guild_id,
                )
                await session.commit()
                series = await session.get(CardSeries, pack.series_id)
        except InsufficientCrystalsError:
            await interaction.followup.send(
                "Not enough Crystals for that series.",
                ephemeral=True,
            )
            return
        except UnknownSeriesError:
            await interaction.followup.send("Series no longer available.", ephemeral=True)
            return
        except SQLAlchemyError:
            _LOG.exception("purchase_with_crystals failed for %s", interaction.user.id)
            await interaction.followup.send(
                "Could not buy that pack. Try again.",
                ephemeral=True,
            )
            return

        series_name = series.display_name if series else "?"
        await interaction.followup.send(
            f"Got a **{series_name}** pack! `{pack.public_id}` — open with "
            f"`/packv card_ref:{pack.public_id}` or `/packcolv`.",
            ephemeral=True,
        )

    async def _handle_buy_random_with_pokedollars(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        ds = DropService()
        ps = PackService()
        try:
            async with self.bot.async_session_factory() as session:
                pack = await ps.purchase_with_pokedollars(
                    session,
                    ds,
                    discord_user_id=interaction.user.id,
                    price=DEFAULT_RANDOM_PACK_PRICE,
                    guild_id=interaction.guild_id,
                )
                await session.commit()
                series = await session.get(CardSeries, pack.series_id)
        except InsufficientPokedollarsError:
            await interaction.followup.send(
                f"You need **{format_pokedollars(DEFAULT_RANDOM_PACK_PRICE)}** to drop a random pack.",
                ephemeral=True,
            )
            return
        except NoActiveSeriesError:
            await interaction.followup.send(
                "No active pack series configured — sync the catalog and restart the bot.",
                ephemeral=True,
            )
            return
        except SQLAlchemyError:
            _LOG.exception("packd Pokedollar purchase failed for user %s", interaction.user.id)
            await interaction.followup.send("Could not drop a pack. Try again.", ephemeral=True)
            return

        series_name = series.display_name if series else "?"
        await interaction.followup.send(
            f"Dropped a random **{series_name}** pack! `{pack.public_id}`\n"
            f"Open it with `/packv card_ref:{pack.public_id}` or `/packcolv`.",
            ephemeral=True,
        )

    async def _handle_buy_random_with_crystals(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        ds = DropService()
        ps = PackService()
        try:
            async with self.bot.async_session_factory() as session:
                pack = await ps.purchase_random_with_crystals(
                    session,
                    ds,
                    discord_user_id=interaction.user.id,
                    crystal_price=DEFAULT_RANDOM_PACK_CRYSTAL_PRICE,
                    guild_id=interaction.guild_id,
                )
                await session.commit()
                series = await session.get(CardSeries, pack.series_id)
        except InsufficientCrystalsError:
            await interaction.followup.send(
                f"You need **{format_crystals(DEFAULT_RANDOM_PACK_CRYSTAL_PRICE)}** to drop a random pack. "
                "Vote with `/vote` or open a pack to earn more.",
                ephemeral=True,
            )
            return
        except NoActiveSeriesError:
            await interaction.followup.send(
                "No active pack series configured — sync the catalog and restart the bot.",
                ephemeral=True,
            )
            return
        except SQLAlchemyError:
            _LOG.exception("packd Crystal purchase failed for user %s", interaction.user.id)
            await interaction.followup.send("Could not drop a pack. Try again.", ephemeral=True)
            return

        series_name = series.display_name if series else "?"
        await interaction.followup.send(
            f"Dropped a random **{series_name}** pack! `{pack.public_id}`\n"
            f"Open it with `/packv card_ref:{pack.public_id}` or `/packcolv`.",
            ephemeral=True,
        )

    async def _handle_open_pack(
        self, interaction: discord.Interaction, pack_instance_id: int
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        ds = DropService()
        ps = PackService()
        try:
            async with self.bot.async_session_factory() as session:
                opened = await ps.open_pack(
                    session,
                    ds,
                    pack_instance_id=pack_instance_id,
                    owner_id=interaction.user.id,
                )
                await session.commit()
        except PackNotFoundError:
            await interaction.followup.send(
                "That pack isn't yours, or no longer exists.",
                ephemeral=True,
            )
            return
        except PackAlreadyOpenedError:
            await interaction.followup.send(
                "That pack was already opened.",
                ephemeral=True,
            )
            return
        except (PackEmptyError, UnknownSeriesError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except SQLAlchemyError:
            _LOG.exception("open_pack failed for instance %s", pack_instance_id)
            await interaction.followup.send(
                "Could not open that pack. Try again.",
                ephemeral=True,
            )
            return

        all_pairs = list(opened.regular) + list(opened.code_cards)
        if not all_pairs:
            await interaction.followup.send(
                "Pack rolled zero cards — please report this.",
                ephemeral=True,
            )
            return

        instance_ids = [inst.id for inst, _ in all_pairs]
        view = OpenedPackFlipView(
            cog=self,
            owner_id=interaction.user.id,
            instance_ids=instance_ids,
        )

        crystal_note = (
            f" — code cards granted **{opened.crystals_credited}** 💎"
            if opened.crystals_credited
            else ""
        )
        intro = (
            f"<@{interaction.user.id}> opened a pack — **{len(all_pairs)} cards** rolled"
            f"{crystal_note}.\n"
            "Use ◀ ▶ to flip; tap **Scrap** to drop a card from your collection."
        )

        # Land directly on the first card's view rather than showing a tiny grid first —
        # ◀ ▶ then walks the rest in `OpenedPackFlipView._render_view`.
        first_inst, first_card = all_pairs[0]
        first_embed = _collection_view_embed(
            first_inst,
            first_card,
            rank_note=f"**1** / **{len(all_pairs)}**",
            collection_owner_id=interaction.user.id,
            viewer_id=interaction.user.id,
        )
        first_content = f"{intro}\n{_card_id_message_line(first_inst.public_id)}"

        try:
            msg = await interaction.followup.send(
                content=first_content,
                embed=first_embed,
                view=view,
                wait=True,
            )
        except discord.HTTPException:
            _LOG.exception("Could not send pack-open initial message; dropping view.")
            return

        view.message = msg

        await _notify_pack_wishlisters(
            self.bot.async_session_factory,
            interaction,
            pairs=all_pairs,
            obtainer_id=interaction.user.id,
        )

    # ----------------------------------------------------------------------- /packd

    @commands.hybrid_command(
        name="packd",
        description="Pack drop: buy a random pack with Pokedollars, Crystals, or the pack SKU. Chat: packd",
    )
    async def pack_drop_cmd(self, ctx: commands.Context) -> None:
        ephe = _hybrid_ephemeral(ctx)
        embed = discord.Embed(title="Random pack drop")
        desc = [
            "Drop a random active-series pack:",
            f"• **{format_pokedollars(DEFAULT_RANDOM_PACK_PRICE)}** Pokedollars, or",
            f"• **{format_crystals(DEFAULT_RANDOM_PACK_CRYSTAL_PRICE)}** Crystals.",
        ]
        if self.bot.settings.discord_pack_consumable_sku_id is not None:
            desc.append("Or use the Discord SKU button below to buy a random pack.")
        embed.description = "\n".join(desc)
        embed.set_footer(text="Use /packv to search and buy a specific series with Crystals (rarer packs cost more).")
        view = PackDropPurchaseView(
            cog=self,
            owner_id=ctx.author.id,
            consumable_sku_id=self.bot.settings.discord_pack_consumable_sku_id,
        )
        msg = await ctx.send(embed=embed, view=view, ephemeral=ephe)
        view.message = msg

    # ----------------------------------------------------------------------- pack series helpers

    async def _send_series_detail(
        self,
        ctx: commands.Context,
        series: CardSeries,
        *,
        ephemeral: bool,
    ) -> None:
        file, art_url = await _resolve_pack_art_attachment(series)
        embed = self._series_embed(series)
        if art_url:
            embed.set_image(url=art_url)
        view = PackSeriesPurchaseView(
            cog=self,
            owner_id=ctx.author.id,
            series=series,
        )
        kwargs: dict = {"embed": embed, "view": view, "ephemeral": ephemeral}
        if file is not None:
            kwargs["file"] = file
        msg = await ctx.send(**kwargs)
        view.message = msg

    async def _list_series_for_packv(
        self,
        ctx: commands.Context,
        *,
        query: str | None,
        ephemeral: bool,
    ) -> None:
        async with self.bot.async_session_factory() as session:
            ps = PackService()
            active = await ps.list_active_series(session)

        if query:
            q = query.strip().lower()
            matches = [
                s
                for s in active
                if q in s.code.lower() or q in s.display_name.lower()
            ]
        else:
            matches = active

        if not matches:
            await ctx.send(
                "No matching packs found. Try a set code like `sv1` or part of the set name.",
                ephemeral=ephemeral,
            )
            return

        exact = None
        if query:
            q = query.strip().lower()
            exact = next((s for s in matches if s.code.lower() == q), None)
            if exact is None and len(matches) == 1:
                exact = matches[0]
        if exact is not None:
            await self._send_series_detail(ctx, exact, ephemeral=ephemeral)
            return

        matches = sorted(matches, key=lambda s: (s.display_name.lower(), s.code))
        matches = matches[:500]
        first = matches[0]
        file, art_url = await _resolve_pack_art_attachment(first)
        embed = self._series_embed(first)
        if art_url:
            embed.set_image(url=art_url)
        embed.description = (embed.description or "") + f"\n\n**1** / **{len(matches)}**"
        view = PackSeriesFlipView(
            cog=self,
            owner_id=ctx.author.id,
            viewer_id=ctx.author.id,
            series_ids=[s.id for s in matches],
            first_series=first,
        )
        kwargs: dict = {"embed": embed, "view": view, "ephemeral": ephemeral}
        if file is not None:
            kwargs["file"] = file
        msg = await ctx.send(**kwargs)
        view.message = msg

    # ----------------------------------------------------------------------- /packv

    @commands.hybrid_command(
        name="packv",
        aliases=["pv"],
        description="Search/view packs to buy, or view an owned pack by ID.",
    )
    @app_commands.describe(
        card_ref="Owned Pack ID, or a series code/search term in chat.",
        series="Pack series code or search text (e.g. sv1, base, Scarlet).",
    )
    async def packv_cmd(
        self,
        ctx: commands.Context,
        card_ref: str | None = None,
        series: str | None = None,
    ) -> None:
        ephe = _hybrid_ephemeral(ctx)
        ps = PackService()
        search_text = (series or card_ref or "").strip() or None

        if card_ref:
            async with self.bot.async_session_factory() as session:
                pack = await ps.get_pack_by_public_id(session, card_ref, ctx.author.id)
                if pack is not None:
                    series_row = await session.get(CardSeries, pack.series_id)
                    file, art_url = await _resolve_pack_art_attachment(series_row)
                    embed = _pack_view_embed(pack, series_row)
                    if art_url:
                        embed.set_image(url=art_url)

                    view = PackViewView(
                        cog=self,
                        pack=pack,
                        owner_id=ctx.author.id,
                    )
                    kwargs: dict = {
                        "content": _pack_id_message_line(pack),
                        "embed": embed,
                        "view": view,
                        "ephemeral": ephe,
                    }
                    if file is not None:
                        kwargs["file"] = file
                    msg = await ctx.send(**kwargs)
                    view.message = msg
                    return

        await self._list_series_for_packv(ctx, query=search_text, ephemeral=ephe)

    # ----------------------------------------------------------------------- /packcolv

    @commands.hybrid_command(
        name="packcolv",
        aliases=["pcpolv"],
        description="Flip through your unopened packs (◀▶) — chat: pcpolv (or packcolv)",
    )
    @app_commands.describe(series="Filter by series code (e.g. sv).")
    async def packcolv_cmd(
        self,
        ctx: commands.Context,
        series: str | None = None,
    ) -> None:
        ephe = _hybrid_ephemeral(ctx)
        ps = PackService()
        async with self.bot.async_session_factory() as session:
            series_id: int | None = None
            if series:
                series_row = await ps.get_series_by_code(session, series)
                if series_row is None:
                    await ctx.send(
                        f"No series with code `{series}` — use `/packv` to search packs.",
                        ephemeral=ephe,
                    )
                    return
                series_id = series_row.id

            packs = await ps.list_unopened_for_user(
                session, discord_user_id=ctx.author.id, series_id=series_id
            )
            if not packs:
                await ctx.send(
                    "You have no unopened packs.\nUse `/packv` to buy a specific pack, or `/packd` for a random drop.",
                    ephemeral=ephe,
                )
                return
            first_pack = packs[0]
            first_series = await session.get(CardSeries, first_pack.series_id)
            file, art_url = await _resolve_pack_art_attachment(first_series)

        embed = _pack_view_embed(first_pack, first_series)
        if art_url:
            embed.set_image(url=art_url)
        embed.description = (embed.description or "") + (
            f"\n\n**1** / **{len(packs)}**"
        )

        view = PackCollectionFlipView(
            cog=self,
            owner_id=ctx.author.id,
            viewer_id=ctx.author.id,
            pack_instance_ids=[p.id for p in packs],
        )

        kwargs: dict = {
            "content": _pack_id_message_line(first_pack),
            "embed": embed,
            "view": view,
            "ephemeral": ephe,
        }
        if file is not None:
            kwargs["file"] = file
        msg = await ctx.send(**kwargs)
        view.message = msg

    # ----------------------------------------------------------------------- /packcat

    @commands.hybrid_command(
        name="packcat",
        description="Browse the pack catalog as a book-flip view of pack art.",
    )
    @app_commands.describe(
        series="Filter by series code or part of the set name (e.g. sv, base, Scarlet).",
        sort="How to order the list.",
        scope="For sort=popular: count packs bought in this server or globally.",
        per_page="Packs shown per page (default 4, max 6).",
    )
    @app_commands.choices(
        sort=[
            app_commands.Choice(name="Default (A→Z by name)", value="default"),
            app_commands.Choice(name="Popular (most opened)", value="popular"),
            app_commands.Choice(name="Rarest top pull", value="rarest"),
            app_commands.Choice(name="Cost: low → high", value="low_cost"),
            app_commands.Choice(name="Cost: high → low", value="high_cost"),
        ],
        scope=[
            app_commands.Choice(name="This server", value="server"),
            app_commands.Choice(name="Global", value="global"),
        ],
    )
    async def packcat_cmd(
        self,
        ctx: commands.Context,
        series: str | None = None,
        sort: str | None = None,
        scope: str | None = None,
        per_page: int = 4,
    ) -> None:
        ephe = _hybrid_ephemeral(ctx)
        sort_key = (sort or "default").strip().lower()
        if sort_key not in {"default", "popular", "rarest", "low_cost", "high_cost"}:
            sort_key = "default"

        # Clamp to a usable range: 1–6. More than 6 tiles makes individual packs
        # too small to read in Discord's embed image preview, especially on mobile.
        try:
            per_page_n = int(per_page)
        except (TypeError, ValueError):
            per_page_n = 4
        per_page_n = max(1, min(6, per_page_n))

        guild_id = ctx.guild.id if ctx.guild else None
        scope_key = (scope or "").strip().lower()
        if scope_key not in {"server", "global"}:
            scope_key = "server" if (sort_key == "popular" and guild_id is not None) else "global"
        if scope_key == "server" and guild_id is None:
            scope_key = "global"

        ps = PackService()
        async with self.bot.async_session_factory() as session:
            active = await ps.list_active_series(session)
            opened = await ps.opened_counts_by_series(
                session,
                guild_id=guild_id if scope_key == "server" else None,
            )
            top_rarity = await ps.top_rarity_by_series(session)
            rarity_rows = await session.execute(select(RarityClass))
            rarity_names = {r.id: r.display_name for r in rarity_rows.scalars()}

        if not active:
            await ctx.send(
                "No active pack series yet — sync the catalog (`python -m poke_pon_bot.scripts.sync_catalog`).",
                ephemeral=ephe,
            )
            return

        query = (series or "").strip().lower()
        if query:
            active = [
                s
                for s in active
                if query in s.code.lower() or query in s.display_name.lower()
            ]
        if not active:
            await ctx.send(
                f"No pack series match `{series}`. Try a TCG set code like `sv1`.",
                ephemeral=ephe,
            )
            return

        if sort_key == "popular":
            active.sort(
                key=lambda s: (
                    -opened.get(s.id, 0),
                    s.display_name.lower(),
                )
            )
        elif sort_key == "rarest":
            active.sort(
                key=lambda s: (
                    -top_rarity.get(s.id, 0),
                    -s.crystal_price,
                    s.display_name.lower(),
                )
            )
        elif sort_key == "low_cost":
            active.sort(key=lambda s: (s.crystal_price, s.display_name.lower()))
        elif sort_key == "high_cost":
            active.sort(key=lambda s: (-s.crystal_price, s.display_name.lower()))
        else:
            active.sort(key=lambda s: (s.display_name.lower(), s.code))

        pages_series: list[list[CardSeries]] = [
            active[i : i + per_page_n] for i in range(0, len(active), per_page_n)
        ]

        sort_label = {
            "default": "A → Z",
            "popular": "most opened",
            "rarest": "rarest top pull",
            "low_cost": "cost low → high",
            "high_cost": "cost high → low",
        }[sort_key]
        scope_label = f" ({scope_key})" if sort_key == "popular" else ""

        view = PackCatalogFlipView(
            cog=self,
            viewer_id=ctx.author.id,
            pages=pages_series,
            per_page=per_page_n,
            sort_label=sort_label,
            scope_label=scope_label,
            filter_label=series if query else None,
            opened_counts=opened,
            top_rarity=top_rarity,
            rarity_names=rarity_names,
            sort_key=sort_key,
        )

        # Hybrid prefix commands don't have a "thinking" state — show the typing
        # indicator while we fetch and composite art so the user isn't left wondering
        # if the command silently no-op'd.
        try:
            file = await view.render_page_attachment()
        except (OSError, ValueError):
            _LOG.exception("packcat strip render failed on first page")
            await ctx.send(
                "Could not render the pack catalog image. Try again in a moment.",
                ephemeral=ephe,
            )
            return

        embed = view.build_embed_for_initial()
        kwargs: dict = {"embed": embed, "view": view, "ephemeral": ephe}
        if file is not None:
            kwargs["file"] = file
        msg = await ctx.send(**kwargs)
        view.message = msg

    # ----------------------------------------------------------------------- entitlement

    @commands.Cog.listener()
    async def on_entitlement_create(self, entitlement: discord.Entitlement) -> None:
        """Grant a pack and consume the entitlement so the user can buy it again."""
        sku_id = self.bot.settings.discord_pack_consumable_sku_id
        if sku_id is None or entitlement.sku_id != sku_id:
            return
        if entitlement.user_id is None:
            _LOG.warning(
                "Pack consumable entitlement %s has no user_id; skipping.", entitlement.id
            )
            return

        ds = DropService()
        ps = PackService()
        try:
            async with self.bot.async_session_factory() as session:
                pack = await ps.grant_consumable_pack(
                    session,
                    ds,
                    discord_user_id=int(entitlement.user_id),
                    guild_id=getattr(entitlement, "guild_id", None),
                )
                await session.commit()
                series = await session.get(CardSeries, pack.series_id)
        except (NoActiveSeriesError, UnknownSeriesError, SQLAlchemyError):
            _LOG.exception(
                "Could not grant consumable pack for entitlement %s; leaving it un-consumed.",
                entitlement.id,
            )
            return

        try:
            await consume_entitlement(self.bot, entitlement_id=int(entitlement.id))
        except (discord.HTTPException, RuntimeError):
            _LOG.exception(
                "Pack granted but consume_entitlement(%s) failed — Discord may not let "
                "the user buy a second copy until this is reconciled.",
                entitlement.id,
            )

        # Notify via DM with the pack ID so the user can immediately use /packv.
        try:
            user = self.bot.get_user(int(entitlement.user_id)) or await self.bot.fetch_user(
                int(entitlement.user_id)
            )
            if user is not None:
                series_name = series.display_name if series is not None else "random"
                await user.send(
                    f"Thanks for your purchase! You got a **{series_name}** pack `{pack.public_id}` — "
                    "open it with `/packv` or `/packcolv`.",
                )
        except (discord.HTTPException, discord.Forbidden):
            _LOG.info("Pack-grant DM blocked for user %s.", entitlement.user_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PacksCog(bot))
    _LOG.info(
        "Loaded packs cog: /packd, /packv, /packcolv, /packcat (+ on_entitlement_create)."
    )
