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
from poke_pon_bot.ui.wishlist_toggle import WishlistToggleButton
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.catalog_card_ref import looks_like_catalog_card_ref
from poke_pon_bot.services.catalog_search import (
    CATALOG_BROWSE_PAGE_CAP,
    any_catalog_content_filter_set,
    any_catalog_filter_set,
    search_catalog,
)
from poke_pon_bot.services.collection_search import (
    _COLV_FLIP_INSTANCE_CAP,
    _CV_FLIP_INSTANCE_CAP,
    any_filter_set,
    list_collection_instance_ids,
    search_collection,
)
from poke_pon_bot.services.collection_visibility import user_instance_not_in_active_auction
from poke_pon_bot.services.cd_drop_themes import (
    format_theme_status_line,
    resolve_active_cd_drop_theme,
)
from poke_pon_bot.services.rarity_luck_boost import (
    format_luck_boost_line,
    get_luck_boost_row,
    resolve_active_rarity_luck_boost,
)
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.config import Settings
from poke_pon_bot.context_reply import reply_target_user_id, resolve_collection_display_target
from poke_pon_bot.error_handlers import reply_command_failure
from poke_pon_bot.services.instance_public_id import compact_public_id_for_line, normalize_public_id
from poke_pon_bot.services.evolution import (
    EvolutionSuccess,
    quote_evolution,
    resolve_evolution_targets,
    run_collection_evolution,
)
from poke_pon_bot.services.pack_collage import render_pack_collage_png
from poke_pon_bot.chat_commands import pp_alias, pp_chat_aliases
from poke_pon_bot.services.referrals import (
    notify_referral_first_pack,
    notify_referral_reward,
    record_referral_cd_use,
)
from poke_pon_bot.services.missions import MissionService, PackMissionAlert
from poke_pon_bot.services.mission_notifications import schedule_mission_completion_dms
from poke_pon_bot.services.engagement_reminders import clear_drop_reminder_after_drop
from poke_pon_bot.services.wallet import WalletService, format_pokedollars
from poke_pon_bot.services.collection_sell import collection_sell_block_reason
from poke_pon_bot.services.instance_favorite import toggle_instance_favorite
from poke_pon_bot.services.wishlist import (
    MAX_WISHLIST_ENTRIES,
    add_wishlist,
    is_wishlisted,
    list_wishlist_card_ids,
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
COLLECTION_WEB_URL = "https://pokepon.org/collection/"


def _collection_web_footer() -> str:
    """Short line appended to collection list/flip replies so players can open the web binder."""
    return (
        f"\n\n_Browse and search your full binder (filters, card details):_\n"
        f"<{COLLECTION_WEB_URL}>"
    )


async def _edit_flip_message(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    files: list[discord.File] | None = None,
) -> None:
    """Edit the flip-browser message whether or not the interaction was deferred."""
    kwargs: dict[str, object] = {"content": content, "embed": embed, "view": view}
    if files:
        kwargs["attachments"] = files
    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
    else:
        await interaction.response.edit_message(**kwargs)


def _patch_embed_rarity(
    embed: discord.Embed,
    printed_name: str | None,
    effective_name: str | None,
) -> None:
    """Swap the Rarity field to graded/effective name when it differs from printed."""
    printed = (printed_name or "—").strip() or "—"
    display = (effective_name or printed).strip() or printed
    value = f"**{display}**\nPrinted: {printed}" if display != printed else display
    for i, field in enumerate(embed.fields):
        if field.name in ("Rarity", "Printed rarity", "Graded rarity"):
            embed.set_field_at(i, name="Rarity", value=value, inline=True)
            return


async def _graded_slab_files(
    session: AsyncSession,
    embed: discord.Embed,
    inst: UserCardInstance,
    card: Card,
) -> list[discord.File]:
    """Replace embed art with a PSA-style slab when this copy is graded."""
    from poke_pon_bot.services.grading import rarity_name_pair

    printed_name, effective_name = await rarity_name_pair(session, card, inst)
    _patch_embed_rarity(embed, printed_name, effective_name)
    if getattr(inst, "grade", None) is None:
        return []
    from poke_pon_bot.services.grading import build_grade_preview
    from poke_pon_bot.services.grade_slab import render_graded_slab_png

    preview = await build_grade_preview(session, inst)
    png = await render_graded_slab_png(
        card,
        grade=int(inst.grade),
        copy_index=preview.copy_index.copy_index,
        total_copies=preview.copy_index.total_copies,
        cert_suffix=inst.public_id,
        enchantment_code=getattr(inst, "grade_enchantment", None),
        rarity_name=effective_name,
    )
    if png is None:
        return []
    embed.set_image(url="attachment://slab.png")
    return [discord.File(png, filename="slab.png")]


def _pack_cards_listing(
    cards: list[Card],
    *,
    chase_slot_indices: frozenset[int] | None = None,
) -> str:
    """Plain-text list of slots with set names (shown on every drop)."""
    if not cards:
        return ""
    chase = chase_slot_indices or frozenset()
    lines = ["**Cards in this pack:**"]
    for i, c in enumerate(cards):
        set_label = (c.set_name or c.set_code or "?").strip()
        num = (c.collector_number or "?").strip()
        rarity = (c.tcg_rarity or "").strip()
        rarity_part = f" · *{rarity}*" if rarity else ""
        chase_mark = " 🎯" if i in chase else ""
        lines.append(
            f"• **#{i + 1}**{chase_mark} {c.name} — **{set_label}** · #{num}{rarity_part}"
        )
    if chase:
        lines.append("_🎯 = rolled from the active **set chase** boost (counts toward the bar)._")
    return "\n".join(lines)


def _pack_mission_block(
    alerts: list[PackMissionAlert],
    *,
    guild: discord.Guild | None,
    private_pack: bool,
    viewer_id: int,
) -> str:
    if not alerts:
        return ""
    if private_pack:
        for alert in alerts:
            if alert.user_id == viewer_id:
                return f"\n\n🎯 **On your mission:** {alert.text}"
        return ""
    if guild is None:
        return ""
    member_ids = {m.id for m in guild.members}
    lines: list[str] = []
    for alert in alerts:
        if alert.user_id not in member_ids:
            continue
        lines.append(f"<@{alert.user_id}> {alert.text}")
    if not lines:
        return ""
    return "\n\n🎯 **Mission match in this drop:**\n" + "\n".join(lines)


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


def _coerce_chat_catalog_name_filter(
    ctx: commands.Context,
    scope: str,
    card_ref: str | None,
    name: str | None,
) -> tuple[str | None, str | None]:
    """Chat ``ppcv g pikachu`` — third token is a name, not ``card_ref`` (catalog id)."""
    if ctx.interaction is not None or scope != "g":
        return card_ref, name
    if card_ref and not name and not looks_like_catalog_card_ref(card_ref):
        return None, card_ref.strip()
    return card_ref, name


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
    """Plain-text Card ID for a **reply** under the embed (mobile: long-press `` `…` `` to copy)."""
    return f"**Card ID:** `{public_id}`"


async def _reply_card_id_below(
    parent: discord.Message,
    public_id: str,
) -> discord.Message:
    """Discord cannot put message text below an embed; a reply sits under the card message."""
    return await parent.reply(
        _card_id_message_line(public_id),
        mention_author=False,
    )


async def _edit_card_id_reply(reply: discord.Message | None, public_id: str) -> None:
    if reply is None:
        return
    try:
        await reply.edit(content=_card_id_message_line(public_id))
    except (discord.NotFound, discord.HTTPException):
        pass


class _CardIdReplyBinding:
    """Mixin for views that keep a copyable Card ID reply in sync when the card changes."""

    _card_id_reply: discord.Message | None

    def _init_card_id_reply(self) -> None:
        self._card_id_reply = None

    def bind_card_id_reply(self, msg: discord.Message) -> None:
        self._card_id_reply = msg

    async def _sync_card_id_reply(self, public_id: str) -> None:
        await _edit_card_id_reply(self._card_id_reply, public_id)


async def _send_collection_card(
    ctx: commands.Context,
    *,
    embed: discord.Embed,
    public_id: str,
    view: discord.ui.View | None = None,
    files: list[discord.File] | None = None,
    content: str | None = None,
    ephemeral: bool = False,
) -> discord.Message:
    """Send collection embed + buttons, then a copyable Card ID reply underneath."""
    send_kw: dict[str, object] = {"embed": embed, "ephemeral": ephemeral}
    if content:
        send_kw["content"] = content
    if view is not None:
        send_kw["view"] = view
    if files:
        send_kw["files"] = files
    msg = await ctx.send(**send_kw)
    reply = await _reply_card_id_below(msg, public_id)
    if view is not None and hasattr(view, "bind_card_id_reply"):
        view.bind_card_id_reply(reply)
    return msg


def _coll_one_line(rank: int, inst: UserCardInstance, card: Card) -> str:
    """Single compact row; Card ID in `` ` `` for tap-to-copy."""
    from poke_pon_bot.services.grading import format_grade_slab_badge

    nm = _truncate(card.name, 22)
    sc = _truncate(card.set_code, 8)
    cn = _truncate(card.collector_number, 10)
    pid = compact_public_id_for_line(inst.public_id)
    grade = int(inst.grade) if getattr(inst, "grade", None) is not None else None
    return f"`{rank}.` **{nm}** `{sc}` #{cn} `{pid}`{format_grade_slab_badge(grade, enchantment_code=getattr(inst, 'grade_enchantment', None))}"


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
    display_rarity: str | None = None,
    printed_rarity: str | None = None,
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
    from poke_pon_bot.services.card_images import card_image_urls

    _small, _large = card_image_urls(card, web_public_url=None)
    e.set_image(url=_large or _small)

    e.add_field(name="Set", value=f"{card.set_name}\n`{card.set_code}`", inline=True)
    e.add_field(name="Card #", value=f"`{card.collector_number}`", inline=True)
    printed = (printed_rarity or card.tcg_rarity or "—").strip() or "—"
    display = (display_rarity or printed).strip() or printed
    if display != printed:
        rarity_value = f"**{display}**\nPrinted: {printed}"
    else:
        rarity_value = display
    e.add_field(name="Rarity", value=rarity_value, inline=True)

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
    if getattr(inst, "is_favorite", False):
        e.add_field(
            name="Favorite",
            value="This copy is **favorited** — sell, trade, and auction are disabled until you unfavorite it.",
            inline=False,
        )
    grade_val = getattr(inst, "grade", None)
    if grade_val is not None:
        from poke_pon_bot.services.grade_enchantments import enchantment_or_default
        from poke_pon_bot.services.grading import grade_label

        e.add_field(
            name="Grade",
            value=f"**{grade_val}** — **{grade_label(int(grade_val))}**",
            inline=True,
        )
        ench = enchantment_or_default(getattr(inst, "grade_enchantment", None))
        e.add_field(name="Enchantment", value=ench.name, inline=True)

    from poke_pon_bot.services.card_roles import format_craft_uses_discord

    uses_dots = format_craft_uses_discord(inst, card)
    if uses_dots:
        e.add_field(
            name="Craft uses",
            value=uses_dots,
            inline=False,
        )

    e.set_footer(text=f"Catalog printing `{card.tcg_card_id}`")
    return e


async def _load_instance_and_card(
    session: AsyncSession,
    instance_id: int,
    owner_discord_id: int,
) -> tuple[UserCardInstance, Card] | None:
    row = await session.execute(
        select(UserCardInstance, Card)
        .join(Card, UserCardInstance.card_id == Card.id)
        .where(
            UserCardInstance.id == instance_id,
            UserCardInstance.discord_user_id == owner_discord_id,
            user_instance_not_in_active_auction(),
        )
    )
    first = row.first()
    if first is None:
        return None
    return (first[0], first[1])


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
            user_instance_not_in_active_auction(),
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


class InstanceFavoriteToggleButton(discord.ui.Button):
    """Favorite this owned copy (locks sell / trade / auction)."""

    def __init__(
        self,
        *,
        session_factory,
        instance_id: int,
        owner_id: int,
        viewer_id: int,
        favorited: bool,
        row: int = 0,
    ) -> None:
        super().__init__(
            emoji="💛" if favorited else "🖤",
            style=discord.ButtonStyle.primary if favorited else discord.ButtonStyle.secondary,
            row=row,
        )
        self._session_factory = session_factory
        self._instance_id = instance_id
        self._owner_id = owner_id
        self._viewer_id = viewer_id
        self._favorited = favorited

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._viewer_id or interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "Only the owner of this copy can favorite it.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            async with self._session_factory() as session:
                new_state = await toggle_instance_favorite(
                    session,
                    discord_user_id=self._owner_id,
                    instance_id=self._instance_id,
                )
                if new_state is None:
                    await interaction.followup.send(
                        "That copy is no longer in your collection.",
                        ephemeral=True,
                    )
                    return
                row = await _load_instance_and_card(session, self._instance_id, self._owner_id)
                await session.commit()
                if row is None:
                    await interaction.followup.send(
                        "That copy is no longer in your collection.",
                        ephemeral=True,
                    )
                    return
                inst, card = row
        except SQLAlchemyError:
            _LOG.exception(
                "favorite toggle instance_id=%s user=%s",
                self._instance_id,
                self._viewer_id,
            )
            await interaction.followup.send("Could not update favorite. Try again.", ephemeral=True)
            return

        self._favorited = bool(new_state)
        self.emoji = "💛" if self._favorited else "🖤"
        self.style = discord.ButtonStyle.primary if self._favorited else discord.ButtonStyle.secondary
        if isinstance(self.view, CollectionFlipView):
            await self.view.refresh_after_favorite(interaction, inst, card)
            return
        if isinstance(self.view, CollectionCardActionsView):
            await self.view.refresh_after_favorite(interaction, inst, card)
            return
        await interaction.response.edit_message(view=self.view)

    def update(self, *, instance_id: int | None = None, favorited: bool | None = None) -> None:
        if instance_id is not None:
            self._instance_id = instance_id
        if favorited is None:
            return
        self._favorited = favorited
        self.emoji = "💛" if favorited else "🖤"
        self.style = discord.ButtonStyle.primary if favorited else discord.ButtonStyle.secondary


class CollectionCardActionsView(_CardIdReplyBinding, discord.ui.View):
    """Wishlist (catalog) + optional favorite (owned copy) for a single card display."""

    def __init__(
        self,
        *,
        session_factory,
        card_id: int,
        viewer_id: int,
        wishlisted: bool,
        instance_id: int | None = None,
        owner_id: int | None = None,
        favorited: bool = False,
        show_favorite: bool = False,
        rank_note: str | None = None,
        collection_owner_id: int | None = None,
    ) -> None:
        super().__init__(timeout=600.0)
        self._init_card_id_reply()
        self._session_factory = session_factory
        self._rank_note = rank_note
        self._collection_owner_id = collection_owner_id
        self._viewer_id = viewer_id
        self.add_item(WishlistToggleButton(
            session_factory=session_factory,
            card_id=card_id,
            viewer_id=viewer_id,
            wishlisted=wishlisted,
            row=0,
        ))
        self._fav_btn: InstanceFavoriteToggleButton | None = None
        if show_favorite and instance_id is not None and owner_id is not None:
            self._fav_btn = InstanceFavoriteToggleButton(
                session_factory=session_factory,
                instance_id=instance_id,
                owner_id=owner_id,
                viewer_id=viewer_id,
                favorited=favorited,
                row=0,
            )
            self.add_item(self._fav_btn)

    async def refresh_after_favorite(
        self,
        interaction: discord.Interaction,
        inst: UserCardInstance,
        card: Card,
    ) -> None:
        embed = _collection_view_embed(
            inst,
            card,
            rank_note=self._rank_note,
            collection_owner_id=self._collection_owner_id,
            viewer_id=self._viewer_id,
        )
        if self._fav_btn is not None:
            self._fav_btn.update(favorited=bool(getattr(inst, "is_favorite", False)))
        async with self._session_factory() as session:
            slab_files = await _graded_slab_files(session, embed, inst, card)
        await _edit_flip_message(
            interaction,
            embed=embed,
            view=self,
            files=slab_files,
        )
        await self._sync_card_id_reply(inst.public_id)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


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


def _single_card_actions_view(
    *,
    session_factory,
    inst: UserCardInstance,
    card: Card,
    viewer_id: int,
    owner_id: int,
    wishlisted: bool,
    rank_note: str | None = None,
    collection_owner_id: int | None = None,
) -> discord.ui.View:
    if owner_id != viewer_id:
        return SingleCardWishlistView(
            session_factory=session_factory,
            card_id=card.id,
            viewer_id=viewer_id,
            wishlisted=wishlisted,
        )
    return CollectionCardActionsView(
        session_factory=session_factory,
        card_id=card.id,
        viewer_id=viewer_id,
        wishlisted=wishlisted,
        instance_id=inst.id,
        owner_id=owner_id,
        favorited=bool(inst.is_favorite),
        show_favorite=True,
        rank_note=rank_note,
        collection_owner_id=collection_owner_id,
    )


class CollectionFlipView(_CardIdReplyBinding, discord.ui.View):
    """Browse owned cards with ◀ ▶, catalog wishlist, and per-copy favorite."""

    def __init__(
        self,
        *,
        session_factory,
        owner_id: int,
        viewer_id: int,
        instance_ids: list[int],
        first_card_id: int = 0,
        first_wishlisted: bool = False,
        first_favorited: bool = False,
        show_favorite: bool = False,
    ) -> None:
        if not instance_ids:
            msg = "instance_ids must be non-empty"
            raise ValueError(msg)
        super().__init__(timeout=600.0)
        self._init_card_id_reply()
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

        self._fav_btn: InstanceFavoriteToggleButton | None = None
        if show_favorite:
            self._fav_btn = InstanceFavoriteToggleButton(
                session_factory=session_factory,
                instance_id=instance_ids[0],
                owner_id=owner_id,
                viewer_id=viewer_id,
                favorited=first_favorited,
                row=0,
            )
            self.add_item(self._fav_btn)

        self._sync_nav_buttons()

    async def refresh_after_favorite(
        self,
        interaction: discord.Interaction,
        inst: UserCardInstance,
        card: Card,
    ) -> None:
        async with self._session_factory() as session:
            wishlisted = await is_wishlisted(
                session, discord_user_id=self._viewer_id, card_id=card.id,
            )
        self._wish_btn.update(card_id=card.id, wishlisted=wishlisted)
        if self._fav_btn is not None:
            self._fav_btn.update(
                instance_id=inst.id,
                favorited=bool(getattr(inst, "is_favorite", False)),
            )
        embed = _collection_view_embed(
            inst,
            card,
            rank_note=self._rank_note(self._index, len(self._instance_ids)),
            collection_owner_id=self._owner_id,
            viewer_id=self._viewer_id,
        )
        self._sync_nav_buttons()
        async with self._session_factory() as session:
            slab_files = await _graded_slab_files(session, embed, inst, card)
        await _edit_flip_message(
            interaction,
            embed=embed,
            view=self,
            files=slab_files,
        )
        await self._sync_card_id_reply(inst.public_id)

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
                slab_files = await _graded_slab_files(session, embed, inst, card)
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
        if self._fav_btn is not None:
            self._fav_btn.update(
                instance_id=inst.id,
                favorited=bool(getattr(inst, "is_favorite", False)),
            )
        self._sync_nav_buttons()
        await _edit_flip_message(
            interaction, embed=embed, view=self, files=slab_files,
        )
        await self._sync_card_id_reply(inst.public_id)


def _inline_reroll_button_row(card_count: int) -> int | None:
    """Action row for the reroll button (to the right of the last claim button when possible)."""
    if card_count < 1:
        return None
    if card_count >= 25 and card_count % 5 == 0:
        return None
    if card_count % 5 == 0:
        return min(card_count // 5, 4)
    return card_count // 5


class CdRerollButton(discord.ui.Button):
    """Issuer-only: reroll one random unclaimed slot (once per pack)."""

    def __init__(self, *, pack_view: "PackPickView", row: int) -> None:
        from poke_pon_bot.services.crystal_sinks import CD_REROLL_CRYSTAL_COST
        from poke_pon_bot.services.crystals import format_crystals

        super().__init__(
            label=_truncate(f"Reroll {format_crystals(CD_REROLL_CRYSTAL_COST)}", 80),
            style=discord.ButtonStyle.primary,
            row=row,
            disabled=pack_view._reroll_used,
        )
        self._pack_view = pack_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._pack_view.handle_reroll_button(interaction)


class IssuerCdRerollView(discord.ui.View):
    """Fallback when the pack grid is full (25 claim rows): single reroll button, ephemeral."""

    def __init__(self, *, pack_view: "PackPickView") -> None:
        remaining = max(30, int(pack_view._deadline_unix) - int(time.time()))
        super().__init__(timeout=float(min(remaining, 300.0)))
        if not pack_view._reroll_used:
            self.add_item(CdRerollButton(pack_view=pack_view, row=0))


class ClaimSlotButton(discord.ui.Button):
    """Claim one revealed slot — first come per slot; per-user grab limit enforced by the view."""

    def __init__(self, *, pick_view: "PackPickView", idx: int, card_name: str, row: int) -> None:
        label = _truncate(f"#{idx + 1} · Take · {card_name}", 80)
        super().__init__(style=discord.ButtonStyle.secondary, label=label, row=row)
        # Do not set Item._parent — discord.py chains _run_checks to _parent (nested Items only).
        self._pick_view = pick_view
        self._idx = idx

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._pick_view.try_claim(interaction, self._idx, self)


class PackPickView(discord.ui.View):
    """Public pack fight: claim slots until the countdown expires (per-user grab cap configurable)."""

    def __init__(
        self,
        *,
        session_factory,
        issuer_id: int,
        opener_mention: str,
        cards: list[Card],
        deadline_unix: int,
        private_pack: bool,
        mission_block: str = "",
        max_grabs_per_user: int = 1,
        claim_seconds: int = _PACK_CLAIM_SECONDS,
        restored_slot_claimer: dict[int, int] | None = None,
        restored_slot_pid: dict[int, str] | None = None,
        guild_id: int | None = None,
        reroll_used: bool = False,
        chase_slot_indices: frozenset[int] | None = None,
    ) -> None:
        claim_seconds = max(30, int(claim_seconds))
        now = int(time.time())
        remaining = max(30, int(deadline_unix) - now)
        super().__init__(timeout=float(remaining))
        self._session_factory = session_factory
        self._issuer_id = issuer_id
        self._opener_mention = opener_mention
        self._cards = cards
        self._deadline_unix = deadline_unix
        self._claim_seconds_total = claim_seconds
        self._private_pack = private_pack
        self._mission_block = mission_block
        self._max_grabs_per_user = max(1, min(int(max_grabs_per_user), len(cards)))
        self._lock = asyncio.Lock()
        # slot_index -> claimer user id
        self._slot_claimer: dict[int, int] = dict(restored_slot_claimer or {})
        # slot_index -> per-claim Card ID (public_id) so we can echo it in plain text
        # outside the embed (mobile long-press copy).
        self._slot_pid: dict[int, str] = dict(restored_slot_pid or {})
        self._finished = False
        self._reroll_used = bool(reroll_used)
        self._chase_slots: set[int] = set(chase_slot_indices or ())
        self._drop_host: commands.Cog | None = None
        self._channel_id: int | None = None
        self._guild_id = guild_id
        self._content_prefix = ""

        for i, card in enumerate(cards):
            row = i // 5
            self.add_item(ClaimSlotButton(pick_view=self, idx=i, card_name=card.name, row=row))
        reroll_row = _inline_reroll_button_row(len(cards))
        if reroll_row is not None:
            self.add_item(CdRerollButton(pack_view=self, row=reroll_row))
        self._apply_restored_slot_buttons()
        if len(self._slot_pid) >= len(self._cards) and self._cards:
            self._finished = True
            for child in self.children:
                child.disabled = True
        elif self._reroll_used:
            for child in self.children:
                if isinstance(child, CdRerollButton):
                    child.disabled = True

    def bind_drop_host(
        self,
        host: commands.Cog,
        *,
        channel_id: int,
        guild_id: int | None,
        content_prefix: str = "",
    ) -> None:
        self._drop_host = host
        self._channel_id = int(channel_id)
        self._guild_id = guild_id
        self._content_prefix = content_prefix or ""

    def _pick_reroll_slot(self) -> int | None:
        import secrets

        open_slots = [i for i in range(len(self._cards)) if i not in self._slot_pid]
        if not open_slots:
            return None
        return secrets.choice(open_slots)

    async def handle_reroll_button(self, interaction: discord.Interaction) -> None:
        from poke_pon_bot.services.crystal_sinks import CD_REROLL_CRYSTAL_COST, reroll_cd_pack_slot
        from poke_pon_bot.services.crystals import CrystalsService, format_crystals

        if interaction.user.id != self._issuer_id:
            await interaction.response.send_message(
                "Only the player who opened this pack can reroll.",
                ephemeral=True,
            )
            return
        if self._reroll_used:
            await interaction.response.send_message(
                "You already used your reroll on this pack.",
                ephemeral=True,
            )
            return
        slot_idx = self._pick_reroll_slot()
        if slot_idx is None:
            await interaction.response.send_message(
                "Every slot is already claimed — nothing left to reroll.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        from poke_pon_bot.services.crystals import InsufficientCrystalsError

        crystals = CrystalsService()
        try:
            async with self._session_factory() as session:
                try:
                    await crystals.try_debit(session, int(self._issuer_id), CD_REROLL_CRYSTAL_COST)
                except InsufficientCrystalsError:
                    bal = await crystals.get_balance(session, int(self._issuer_id))
                    await interaction.followup.send(
                        f"Reroll costs {format_crystals(CD_REROLL_CRYSTAL_COST)} "
                        f"(you have {format_crystals(bal)}).",
                        ephemeral=True,
                    )
                    return
                draw = await reroll_cd_pack_slot(session, guild_id=self._guild_id)
                new_card = draw.card
                self._cards[slot_idx] = new_card
                if draw.from_set_chase:
                    self._chase_slots.add(slot_idx)
                else:
                    self._chase_slots.discard(slot_idx)
                self._reroll_used = True
                for child in self.children:
                    if isinstance(child, ClaimSlotButton) and child._idx == slot_idx:
                        child.label = _truncate(
                            f"#{slot_idx + 1} · Take · {new_card.name}", 80
                        )
                    if isinstance(child, CdRerollButton):
                        child.disabled = True
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("cd reroll failed issuer=%s", self._issuer_id)
            await interaction.followup.send("Could not reroll — try again.", ephemeral=True)
            return

        note = (
            f"\n\n🔄 **{interaction.user.mention}** rerolled slot **#{slot_idx + 1}** "
            f"→ **{new_card.name}** ({format_crystals(CD_REROLL_CRYSTAL_COST)})."
        )
        try:
            png = await render_pack_collage_png(self._cards)
        except (OSError, ValueError, httpx.HTTPError):
            png = None
        if self.message is not None:
            try:
                if png is not None:
                    file = discord.File(png, filename="pack.png")
                    await self.message.edit(
                        content=self.full_content(note),
                        attachments=[file],
                        view=self,
                    )
                else:
                    await self.message.edit(content=self.full_content(note), view=self)
            except (discord.HTTPException, discord.NotFound):
                _LOG.debug("Could not edit pack after reroll", exc_info=True)
        self._schedule_persist()
        await interaction.followup.send(
            f"Rerolled **#{slot_idx + 1}** to **{new_card.name}**.",
            ephemeral=True,
        )

    def _apply_restored_slot_buttons(self) -> None:
        stale = [i for i in self._slot_claimer if i not in self._slot_pid]
        for i in stale:
            self._slot_claimer.pop(i, None)
        for child in self.children:
            if not isinstance(child, ClaimSlotButton):
                continue
            idx = child._idx
            if idx not in self._slot_pid:
                continue
            card = self._cards[idx] if 0 <= idx < len(self._cards) else None
            name = card.name if card is not None else "Card"
            child.disabled = True
            child.style = discord.ButtonStyle.success
            child.label = _truncate(f"#{idx + 1} · Taken · {name}", 80)
        if self._reroll_used or self._finished:
            for child in self.children:
                if isinstance(child, CdRerollButton):
                    child.disabled = True

    def full_content(self, extra: str = "") -> str:
        body = self.build_content() + (extra or "")
        prefix = (self._content_prefix or "").strip()
        if prefix:
            return f"{prefix}\n\n{body}"
        return body

    def _schedule_persist(self) -> None:
        host = self._drop_host
        if host is None or self._private_pack or self.message is None:
            return
        host.schedule_drop_persist(self)

    def _user_grab_count(self, user_id: int) -> int:
        return sum(1 for uid in self._slot_claimer.values() if uid == user_id)

    _DISCORD_CONTENT_LIMIT = 2000
    _CONTENT_SAFETY_BUFFER = 60

    def _compact_drop_summary(self) -> str:
        """Short status once claims start — frees space for the full claim list.

        Card names stay in the collage image; repeating all 25 lines in text
        on every grab was blowing the 2000-char cap and hiding who claimed what.
        """
        n = len(self._cards)
        claimed = len(self._slot_pid)
        unclaimed = [i + 1 for i in range(n) if i not in self._slot_pid]
        lines = [
            f"**{n}** cards in this drop (see collage). "
            f"**{claimed}** / **{n}** claimed."
        ]
        if unclaimed and not self._finished:
            if len(unclaimed) <= 18:
                open_slots = ", ".join(f"#{s}" for s in unclaimed)
            else:
                open_slots = f"{len(unclaimed)} slots"
            lines.append(f"**Still open:** {open_slots}")
        return "\n".join(lines)

    def build_content(self) -> str:
        expiry = f"**Expires** <t:{self._deadline_unix}:R>"
        grab_rule = (
            f"Each person may take up to **{self._max_grabs_per_user}** card(s)."
            if self._max_grabs_per_user > 1
            else "Each person may take **one** card."
        )
        if self._private_pack:
            base = (
                f"{self._opener_mention} opened a pack (**private** — only you see this).\n"
                f"{expiry}\n"
                f"{grab_rule} Tap **Take** on a slot to save it."
            )
        else:
            base = (
                f"{self._opener_mention} dropped cards, it's up for grabs!\n"
                f"{expiry}\n"
                f"{grab_rule}"
            )
        if not self._slot_pid:
            parts = [
                base,
                _pack_cards_listing(
                    self._cards,
                    chase_slot_indices=frozenset(self._chase_slots),
                ),
            ]
            if self._mission_block:
                parts.append(self._mission_block.strip())
            return "\n\n".join(parts)
        parts = [base, _pack_cards_listing(self._cards)]
        if self._mission_block:
            parts.append(self._mission_block.strip())
        return self._compose_with_claims(parts)

    def _compose_with_claims(self, parts: list[str]) -> str:
        """Append every claimed slot; trim only if still over Discord's 2000-char limit."""
        header = "**Claimed** (long-press IDs to copy):"
        ordered_slots = sorted(self._slot_pid)

        def line_for(slot: int) -> str:
            uid = self._slot_claimer.get(slot)
            who = f"<@{uid}>" if uid is not None else "?"
            name = self._cards[slot].name if 0 <= slot < len(self._cards) else ""
            name_part = f" · {name}" if name else ""
            return f"• #{slot + 1} {who}{name_part} · `{self._slot_pid[slot]}`"

        claim_lines = [line_for(s) for s in ordered_slots]
        budget = self._DISCORD_CONTENT_LIMIT - self._CONTENT_SAFETY_BUFFER

        def assemble(include_mission: bool, use_compact: bool, lines: list[str]) -> str:
            body_parts = list(parts)
            if not include_mission:
                body_parts = [p for p in body_parts if p != (self._mission_block or "").strip()]
            if use_compact:
                # Replace full card listing with compact summary when needed
                full_listing = _pack_cards_listing(self._cards)
                body_parts = [
                    self._compact_drop_summary() if p == full_listing else p
                    for p in body_parts
                ]
            return "\n\n".join(body_parts + ["\n".join([header, *lines])])

        # Try with full card listing and mission block
        candidate = assemble(True, False, claim_lines)
        if len(candidate) <= budget:
            return candidate
        
        # Try without mission block but keep full card listing
        candidate = assemble(False, False, claim_lines)
        if len(candidate) <= budget:
            return candidate
        
        # Try with compact summary instead of full card listing
        candidate = assemble(False, True, claim_lines)
        if len(candidate) <= budget:
            return candidate

        # Last resort: compact summary + trim old claim lines
        head_text = assemble(False, True, []) + "\n"
        remaining = budget - len(head_text)
        kept_lines: list[str] = []
        used = 0
        hidden = 0
        for slot in reversed(ordered_slots):
            ln = line_for(slot)
            cost = len(ln) + (1 if kept_lines else 0)
            if used + cost <= remaining - 90:
                kept_lines.append(ln)
                used += cost
            else:
                hidden += 1
        kept_lines.reverse()
        if hidden:
            note = (
                f"_Showing the latest claims — **{hidden}** earlier hidden "
                f"(**{len(ordered_slots)}** total). Discord 2000-char limit._"
            )
            kept_lines.insert(0, note)
        return head_text + "\n".join(kept_lines)

    async def _reply_ephemeral(self, interaction: discord.Interaction, text: str) -> None:
        """Send a private notice using whichever response channel is still valid."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            _LOG.warning("Drop ephemeral reply failed for user=%s", interaction.user.id)

    async def try_claim(self, interaction: discord.Interaction, idx: int, button: discord.ui.Button) -> None:
        uid = interaction.user.id
        card = self._cards[idx] if 0 <= idx < len(self._cards) else None

        # ---- Phase 1: validate + reserve the slot (under lock, no Discord I/O). ----
        reject: str | None = None
        async with self._lock:
            if self._finished:
                reject = "This pack is already finished."
            elif int(time.time()) >= self._deadline_unix:
                reject = "This pack has expired."
            elif interaction.user.bot:
                reject = "Bots can’t claim cards."
            elif card is None:
                reject = "That slot no longer exists."
            else:
                grabs = self._user_grab_count(uid)
                if grabs >= self._max_grabs_per_user:
                    limit = self._max_grabs_per_user
                    reject = (
                        "You already claimed **one** card from this pack."
                        if limit == 1
                        else f"You already claimed **{limit}** card(s) from this pack "
                        "(your limit for this drop)."
                    )
                elif idx in self._slot_claimer:
                    reject = "That slot was already taken."
                else:
                    # Tentative reservation — release lock before Discord/DB I/O.
                    self._slot_claimer[idx] = uid

        if reject is not None:
            await self._reply_ephemeral(interaction, reject)
            return

        # Ack within Discord's ~3s window before DB / mission / chase writes.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.NotFound:
            _LOG.warning(
                "claim interaction already expired user=%s slot=%s; releasing reservation",
                uid,
                idx,
            )
            async with self._lock:
                if self._slot_claimer.get(idx) == uid:
                    self._slot_claimer.pop(idx, None)
            return
        except discord.HTTPException:
            _LOG.exception("claim defer failed user=%s slot=%s", uid, idx)
            async with self._lock:
                if self._slot_claimer.get(idx) == uid:
                    self._slot_claimer.pop(idx, None)
            return

        # ---- Phase 2: DB work outside the lock. ----
        drop_result = None
        mission_notices: list = []
        try:
            async with self._session_factory() as session:
                db_card = await session.get(Card, card.id)
                if db_card is None:
                    async with self._lock:
                        if self._slot_claimer.get(idx) == uid:
                            self._slot_claimer.pop(idx, None)
                    await self._reply_ephemeral(
                        interaction, "That card no longer exists in the catalog."
                    )
                    return
                drop_result = await DropService().claim_card(
                    session,
                    discord_user_id=uid,
                    card=db_card,
                    source="drop",
                )
                try:
                    mission_notices = await MissionService().record_drop_claim(
                        session, uid, db_card,
                    )
                except Exception:
                    _LOG.exception("mission record_drop_claim failed user=%s", uid)
                    mission_notices = []
                try:
                    from poke_pon_bot.services.set_chase import record_set_chase_claim

                    await record_set_chase_claim(session, discord_user_id=uid, card=db_card)
                except Exception:
                    _LOG.exception("set chase record failed user=%s", uid)
                await session.commit()
        except Exception:
            _LOG.exception("drop claim db error user=%s card=%s", uid, card.id)
            async with self._lock:
                if self._slot_claimer.get(idx) == uid:
                    self._slot_claimer.pop(idx, None)
            await self._reply_ephemeral(
                interaction, "Could not save that card — please try again."
            )
            return

        # ---- Phase 3: finalize view + update the deferred interaction. ----
        async with self._lock:
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
                msg_obj = getattr(self, "message", None)
                if self._drop_host is not None and msg_obj is not None:
                    asyncio.create_task(
                        self._drop_host.clear_drop_persist(int(msg_obj.id), delete_row=True),
                        name=f"drop-clear-{msg_obj.id}",
                    )
            extra = (
                f"\n\n✅ **All {len(self._cards)} card(s) claimed.**" if all_taken else ""
            )
            new_content = self.full_content(extra)

        edited = False
        try:
            await interaction.edit_original_response(content=new_content, view=self)
            edited = True
        except discord.NotFound:
            _LOG.warning(
                "interaction token expired for user=%s claim; trying message.edit",
                uid,
            )
        except discord.HTTPException:
            _LOG.exception("edit_original_response failed for user=%s claim", uid)

        if not edited:
            msg_obj = getattr(self, "message", None) or interaction.message
            if msg_obj is not None:
                try:
                    await msg_obj.edit(content=new_content, view=self)
                except (discord.NotFound, discord.HTTPException):
                    _LOG.exception(
                        "message.edit fallback also failed for user=%s claim", uid
                    )

        # ---- Phase 4: post-claim hooks (must not break the claim). ----
        if mission_notices:
            mission_svc = MissionService()
            schedule_mission_completion_dms(
                interaction.client, user_id=uid, notices=mission_notices
            )
            for notice in mission_notices:
                try:
                    await interaction.followup.send(
                        mission_svc.format_progress_notice(notice),
                        ephemeral=False,
                    )
                except (discord.NotFound, discord.HTTPException):
                    pass

        try:
            await _notify_wishlisters(
                self._session_factory,
                interaction,
                card_id=card.id,
                card_name=card.name,
                obtainer_id=uid,
            )
        except Exception:
            _LOG.exception("wishlist notify failed user=%s card=%s", uid, card.id)

        if self._drop_host is not None:
            try:
                from poke_pon_bot.services.tutorial import notify_drop_claimed_for_tutorial

                await notify_drop_claimed_for_tutorial(self._drop_host.bot, uid)
            except Exception:
                _LOG.exception("tutorial drop-claim hook failed user=%s", uid)

        self._schedule_persist()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        _LOG.exception(
            "PackPickView item=%s raised for user=%s",
            getattr(item, "label", item),
            interaction.user.id,
            exc_info=error,
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong claiming that card — please try again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong claiming that card — please try again.",
                    ephemeral=True,
                )
        except (discord.NotFound, discord.HTTPException):
            pass

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        self._finished = True
        msg = getattr(self, "message", None)
        mid = int(msg.id) if msg is not None else None
        host = self._drop_host
        extra = (
            "\n\n⏱ **Time's up** — unclaimed cards are gone.\n"
            f"_Claimed **{len(self._slot_pid)}** / **{len(self._cards)}**._"
        )
        if msg is not None:
            try:
                await msg.edit(content=self.full_content(extra), view=self)
            except (discord.NotFound, discord.HTTPException):
                pass
        if host is not None and mid is not None:
            await host.clear_drop_persist(mid, delete_row=True)


_SCOPE = [
    app_commands.Choice(name="Global catalog (g)", value="g"),
    app_commands.Choice(name="My collection (c)", value="c"),
]


class GachaCog(commands.Cog):
    """Weighted drops; chat commands (`ppcd`, `ppcs`, `ppcv`, `ppcolv`, `ppcevolve`) plus slash equivalents."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wallet = WalletService()
        self._settings: Settings = bot.settings
        self._drop_boost_cache: dict[int, tuple[bool, float]] = {}
        self._active_drop_views: dict[int, PackPickView] = {}
        self._drops_restored = False

    def register_drop_view(self, view: PackPickView, *, message_id: int) -> None:
        self._active_drop_views[int(message_id)] = view

    def schedule_drop_persist(self, view: PackPickView) -> None:
        asyncio.create_task(
            self._persist_drop_view(view),
            name=f"drop-persist-{getattr(view.message, 'id', '?')}",
        )

    async def _persist_drop_view(self, view: PackPickView) -> None:
        from poke_pon_bot.services.drop_recovery import upsert_pending_drop

        if view._private_pack or view.message is None or view._channel_id is None:
            return
        try:
            async with self.bot.async_session_factory() as session:
                await upsert_pending_drop(
                    session,
                    view,
                    channel_id=int(view._channel_id),
                    guild_id=view._guild_id,
                    content_prefix=view._content_prefix,
                )
                await session.commit()
        except Exception:
            _LOG.exception("persist channel drop failed msg=%s", getattr(view.message, "id", None))

    async def clear_drop_persist(self, message_id: int, *, delete_row: bool = True) -> None:
        from poke_pon_bot.services.drop_recovery import delete_pending_drop

        self._active_drop_views.pop(int(message_id), None)
        if not delete_row:
            return
        try:
            async with self.bot.async_session_factory() as session:
                await delete_pending_drop(session, int(message_id))
                await session.commit()
        except Exception:
            _LOG.exception("clear pending drop failed msg=%s", message_id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._drops_restored:
            return
        self._drops_restored = True
        await self.restore_channel_drops()

    async def restore_channel_drops(self) -> None:
        from poke_pon_bot.services.drop_recovery import (
            _slot_maps_to_int,
            finalize_expired_drop_row,
            list_expired_pending_drops,
            list_restorable_drops,
            load_cards_for_drop,
        )

        restored = 0
        expired = 0
        async with self.bot.async_session_factory() as session:
            for row in await list_expired_pending_drops(session):
                await finalize_expired_drop_row(session, row, self.bot)
                expired += 1
            rows = await list_restorable_drops(session)
            for row in rows:
                remaining = int(row.deadline_unix) - int(time.time())
                if remaining <= 0:
                    await finalize_expired_drop_row(session, row, self.bot)
                    expired += 1
                    continue
                cards = await load_cards_for_drop(session, list(row.card_ids))
                if not cards:
                    await finalize_expired_drop_row(session, row, self.bot)
                    expired += 1
                    continue
                claimer, pids = _slot_maps_to_int(
                    dict(row.slot_claimer or {}),
                    dict(row.slot_public_ids or {}),
                )
                view = PackPickView(
                    session_factory=self.bot.async_session_factory,
                    issuer_id=int(row.issuer_id),
                    opener_mention=str(row.opener_mention),
                    cards=cards,
                    deadline_unix=int(row.deadline_unix),
                    private_pack=False,
                    mission_block=str(row.mission_block or ""),
                    max_grabs_per_user=int(row.max_grabs_per_user),
                    claim_seconds=int(row.claim_seconds),
                    restored_slot_claimer=claimer,
                    restored_slot_pid=pids,
                    guild_id=row.guild_id,
                    reroll_used=bool(getattr(row, "reroll_used", False)),
                )
                view.bind_drop_host(
                    self,
                    channel_id=int(row.channel_id),
                    guild_id=row.guild_id,
                    content_prefix=str(row.content_prefix or ""),
                )
                self.bot.add_view(view, message_id=int(row.message_id))
                self.register_drop_view(view, message_id=int(row.message_id))
                try:
                    channel = self.bot.get_channel(int(row.channel_id))
                    if channel is None:
                        channel = await self.bot.fetch_channel(int(row.channel_id))
                    if channel is not None:
                        view.message = await channel.fetch_message(int(row.message_id))
                except (discord.NotFound, discord.HTTPException):
                    _LOG.warning(
                        "Restored drop buttons for msg=%s but could not fetch message",
                        row.message_id,
                    )
                restored += 1
            await session.commit()
        if restored or expired:
            _LOG.info(
                "Channel drop recovery: restored %s active drop(s), finalized %s expired.",
                restored,
                expired,
            )

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
        try:
            from poke_pon_bot.services.stripe_shop import user_has_stripe_drop_boost

            async with self.bot.async_session_factory() as session:
                if await user_has_stripe_drop_boost(session, discord_user_id=user_id):
                    return True
        except Exception:
            _LOG.debug("stripe drop boost lookup failed user=%s", user_id, exc_info=True)
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
        favorited_only: bool = False,
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
                    slab_files = await _graded_slab_files(session, embed, inst, card)
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
            view = _single_card_actions_view(
                session_factory=self.bot.async_session_factory,
                inst=inst,
                card=card,
                viewer_id=viewer_id,
                owner_id=owner_id,
                wishlisted=wishlisted,
                rank_note="**cv c** by **card_ref** (Card ID).",
                collection_owner_id=owner_id if peer else None,
            )
            await _send_collection_card(
                ctx,
                embed=embed,
                public_id=inst.public_id,
                view=view,
                files=slab_files,
                ephemeral=ephe,
            )
            return

        if not any_filter_set(
            name_contains=name,
            rarity_contains=rarity,
            pokedex=pokedex,
            slot=slot,
            public_id=None,
            favorited_only=favorited_only,
        ):
            await ctx.send(
                f"Use **`slot`** (e.g. **1** for newest match), **`card_ref`** (**Card ID**), **`favorited`**, and/or "
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
                    favorited_only=favorited_only,
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
                    favorited_only=favorited_only,
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
                slab_files = await _graded_slab_files(s2, embed, inst, card)
            view = _single_card_actions_view(
                session_factory=self.bot.async_session_factory,
                inst=inst,
                card=card,
                viewer_id=viewer_id,
                owner_id=owner_id,
                wishlisted=w,
                rank_note=note,
                collection_owner_id=owner_id if peer else None,
            )
            await _send_collection_card(
                ctx,
                embed=embed,
                public_id=inst.public_id,
                view=view,
                files=slab_files,
                ephemeral=ephe,
            )
            return

        if total > 1 and slot is None:
            try:
                async with self.bot.async_session_factory() as session:
                    ids, flip_total = await list_collection_instance_ids(
                        session,
                        discord_user_id=owner_id,
                        name_contains=name,
                        rarity_contains=rarity,
                        pokedex=pokedex,
                        favorited_only=favorited_only,
                    )
                    if not ids:
                        await ctx.send(
                            "Could not load matching cards. Try again.",
                            ephemeral=ephe,
                        )
                        return
                    row0 = await _load_instance_and_card(session, ids[0], owner_id)
                    if row0 is None:
                        await ctx.send(
                            "Could not load that collection. Try again.",
                            ephemeral=ephe,
                        )
                        return
                    inst0, card0 = row0
                    first_wishlisted = await is_wishlisted(
                        session, discord_user_id=viewer_id, card_id=card0.id,
                    )
                    filt_note = " · **favorited**" if favorited_only else ""
                    cap_note = ""
                    if flip_total > len(ids):
                        cap_note = (
                            f" Showing **{len(ids)}** of **{flip_total}** matches"
                            f" (cap **{_CV_FLIP_INSTANCE_CAP}**)."
                        )
                    rank_note = (
                        f"**cv c** flip — **{len(ids)}** match(es){filt_note} (newest first).{cap_note}"
                    )
                    embed = _collection_view_embed(
                        inst0,
                        card0,
                        rank_note=rank_note,
                        collection_owner_id=owner_id,
                        viewer_id=viewer_id,
                    )
                    slab_files = await _graded_slab_files(session, embed, inst0, card0)
            except SQLAlchemyError:
                _LOG.exception("cv c flip browse owner %s viewer %s", owner_id, viewer_id)
                await ctx.send("Could not load that collection. Try again.", ephemeral=ephe)
                return

            view = CollectionFlipView(
                session_factory=self.bot.async_session_factory,
                owner_id=owner_id,
                viewer_id=viewer_id,
                instance_ids=ids,
                first_card_id=card0.id,
                first_wishlisted=first_wishlisted,
                first_favorited=bool(inst0.is_favorite),
                show_favorite=(owner_id == viewer_id),
            )
            await _send_collection_card(
                ctx,
                embed=embed,
                public_id=inst0.public_id,
                view=view,
                files=slab_files,
                content=_collection_web_footer().lstrip(),
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
            slab_files = await _graded_slab_files(s2, embed, inst, card)
        view = _single_card_actions_view(
            session_factory=self.bot.async_session_factory,
            inst=inst,
            card=card,
            viewer_id=viewer_id,
            owner_id=owner_id,
            wishlisted=w,
            rank_note=(
                "Only one card matched those filters."
                if peer
                else "Only one card matched your filters."
            ),
            collection_owner_id=owner_id if peer else None,
        )
        await _send_collection_card(
            ctx,
            embed=embed,
            public_id=inst.public_id,
            view=view,
            files=slab_files,
            ephemeral=ephe,
        )

    @commands.hybrid_command(
        name="cs",
        aliases=[pp_alias("cs")],
        description="Search: global (g) or your collection (c) — chat: ppcs",
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
        favorited="**c** only — limit list to copies you starred (💛)",
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
        favorited: bool | None = None,
        limit: app_commands.Range[int, 1, 25] = 15,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        if scope not in ("g", "c"):
            await ctx.send("**scope** must be **g** (global catalog) or **c** (your collection).", ephemeral=False)
            return
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        card_ref, name = _coerce_chat_catalog_name_filter(ctx, scope, card_ref, name)
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
        fav_only = bool(favorited) if scope == "c" else False
        if not any_filter_set(
            name_contains=name,
            rarity_contains=rarity,
            pokedex=pokedex,
            slot=slot,
            public_id=pub,
            favorited_only=fav_only,
        ):
            await ctx.send(
                "Add at least one of **`name`**, **`rarity`**, **`pokedex`**, **`slot`**, **`favorited`**, or **`card_ref`** (Card ID).",
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
                    favorited_only=fav_only,
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
        aliases=[pp_alias("cv")],
        description="Card view (g) or your copy (c) — chat: ppcv",
    )
    @app_commands.choices(scope=_SCOPE)
    @app_commands.describe(
        scope="**g** = catalog printing · **c** = your copy",
        card_ref="**g** = exact catalog id (`sv1-112`) · **c** = your **Card ID**",
        slot="**g** / **c**: 1 = first in match list (order differs by scope; see /help or chelp)",
        name="**g** / **c**",
        rarity="**g** / **c**",
        pokedex="**g** / **c**",
        favorited="**c** only — copies you starred (💛)",
        wishlist="**g** only — catalog printings on your wishlist (⭐)",
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
        favorited: bool | None = None,
        wishlist: bool | None = None,
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
        card_ref, name = _coerce_chat_catalog_name_filter(ctx, scope, card_ref, name)
        viewer_id = ctx.author.id
        reply_owner = await reply_target_user_id(self.bot, ctx)
        owner_id = reply_owner if reply_owner is not None else viewer_id
        fav_only = bool(favorited)
        wl_only = bool(wishlist)
        if scope == "c":
            if wl_only:
                await ctx.send(
                    "**Wishlist** is for **`cv g`** (catalog). Use **`favorited`** on **`cv c`** for copies you starred.",
                    ephemeral=ephe,
                )
                return
            await self._collection_view_execute(
                ctx,
                owner_id=owner_id,
                viewer_id=viewer_id,
                slot=slot,
                name=name,
                rarity=rarity,
                pokedex=pokedex,
                card_ref=card_ref,
                favorited_only=fav_only,
            )
            return
        if fav_only:
            await ctx.send(
                "**Favorited** is for **`cv c`** (your copies). Use **`wishlist`** on **`cv g`** for catalog stars.",
                ephemeral=ephe,
            )
            return
        tcg = card_ref.strip() if card_ref and card_ref.strip() else None
        if wl_only:
            try:
                async with self.bot.async_session_factory() as session:
                    card_ids, wl_total = await list_wishlist_card_ids(
                        session, viewer_id, max_ids=CATALOG_BROWSE_PAGE_CAP,
                    )
            except SQLAlchemyError:
                _LOG.exception("cv g wishlist for user %s", viewer_id)
                await ctx.send("Could not load your wishlist. Try again later.", ephemeral=ephe)
                return
            if wl_total == 0:
                await ctx.send(
                    "Your **wishlist** is empty — star catalog cards with **⭐** on **`cv g`** views.",
                    ephemeral=ephe,
                )
                return
            if slot is not None and int(slot) > wl_total:
                await ctx.send(
                    f"Only **{wl_total}** wishlisted printing(s); **slot** must be **1**–**{wl_total}**.",
                    ephemeral=ephe,
                )
                return
            start_idx = 0 if slot is None else int(slot) - 1
            if not (0 <= start_idx < len(card_ids)):
                await ctx.send("Invalid **slot** for your wishlist.", ephemeral=ephe)
                return
            show_id = card_ids[start_idx]
            try:
                async with self.bot.async_session_factory() as session:
                    card0 = await session.get(Card, show_id)
                    if card0 is None:
                        await ctx.send("That wishlisted card is no longer in the catalog.", ephemeral=ephe)
                        return
            except SQLAlchemyError:
                _LOG.exception("cv g wishlist card load")
                await ctx.send("Could not load that card. Try again later.", ephemeral=ephe)
                return
            view = CatalogBrowseView(
                session_factory=self.bot.async_session_factory,
                owner_id=viewer_id,
                card_ids=card_ids,
                search_total=wl_total,
                first_wishlisted=True,
            )
            view.set_index(start_idx, wishlisted=True)
            note = (
                f"Wishlist **#{start_idx + 1}** of **{len(card_ids)}** shown"
                + (f" (**{wl_total}** total)" if wl_total > len(card_ids) else "")
                + " — newest starred first."
            )
            await ctx.send(
                embed=_catalog_view_embed(card0, rank_note=note),
                view=view,
                ephemeral=ephe,
            )
            return
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
            try:
                async with self.bot.async_session_factory() as session:
                    wishlisted = await is_wishlisted(
                        session, discord_user_id=ctx.author.id, card_id=card0.id
                    )
            except SQLAlchemyError:
                wishlisted = False
            view = SingleCardWishlistView(
                session_factory=self.bot.async_session_factory,
                card_id=card0.id,
                viewer_id=ctx.author.id,
                wishlisted=wishlisted,
            )
            await ctx.send(
                embed=_catalog_view_embed(card0, rank_note=note),
                view=view,
                ephemeral=ephe,
            )
            return
        start_idx = 0 if slot is None else int(slot) - 1
        if not (0 <= start_idx < len(window_rows)):
            await ctx.send("Invalid **slot** for this result set.", ephemeral=ephe)
            return
        card_ids = [c.id for c in window_rows]
        show_card = window_rows[start_idx]
        psize = len(card_ids)
        try:
            async with self.bot.async_session_factory() as session:
                wishlisted = await is_wishlisted(
                    session, discord_user_id=ctx.author.id, card_id=show_card.id
                )
        except SQLAlchemyError:
            wishlisted = False
        view = CatalogBrowseView(
            session_factory=self.bot.async_session_factory,
            owner_id=ctx.author.id,
            card_ids=card_ids,
            search_total=total,
            first_wishlisted=wishlisted,
        )
        view.set_index(start_idx, wishlisted=wishlisted)
        note = CatalogBrowseView._note(view._index, psize, total)
        await ctx.send(
            embed=_catalog_view_embed(show_card, rank_note=note),
            view=view,
            ephemeral=ephe,
        )

    @commands.hybrid_command(
        name="colv",
        aliases=[pp_alias("colv")],
        description="Flip through your collection (◀▶) — chat: ppolv",
    )
    @app_commands.describe(
        member="Whose collection to browse — omit for yours",
        limit="Cap cards to flip through, newest first (default **500**, max **1000**)",
    )
    async def collection_flip(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        limit: app_commands.Range[int, 1, 1000] | None = None,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        try:
            viewer_id = ctx.author.id
            target = await resolve_collection_display_target(self.bot, ctx, member_param=member)
            if target.bot:
                await ctx.send("Bots don’t have collections.", ephemeral=ephe)
                return
            owner_id = int(target.id)
            fetch_cap = (
                min(int(limit), _COLV_FLIP_INSTANCE_CAP)
                if limit is not None
                else _CV_FLIP_INSTANCE_CAP
            )
            async with self.bot.async_session_factory() as session:
                ids, coll_total = await list_collection_instance_ids(
                    session,
                    discord_user_id=owner_id,
                    max_ids=fetch_cap,
                )
                if not ids:
                    if owner_id != viewer_id:
                        await ctx.send(
                            f"<@{owner_id}> has an empty collection. **`ppcd`**",
                            ephemeral=ephe,
                        )
                    else:
                        await ctx.send("You have an empty collection. **`ppcd`**", ephemeral=ephe)
                    return
                row0 = await _load_instance_and_card(session, ids[0], owner_id)
                if row0 is None:
                    await ctx.send("Could not load that collection. Try again.", ephemeral=ephe)
                    return
                inst0, card0 = row0
                rank_note = CollectionFlipView._rank_note(0, len(ids))
                if coll_total > len(ids):
                    rank_note += (
                        f" · showing **{len(ids)}** of **{coll_total}**"
                        f" (cap **{fetch_cap}**; use **`limit`** to raise)"
                    )
                embed = _collection_view_embed(
                    inst0,
                    card0,
                    rank_note=rank_note,
                    collection_owner_id=owner_id,
                    viewer_id=viewer_id,
                )
                slab_files = await _graded_slab_files(session, embed, inst0, card0)
                first_card_id = int(card0.id)
                first_wishlisted = await is_wishlisted(
                    session, discord_user_id=viewer_id, card_id=card0.id,
                )
                first_favorited = bool(getattr(inst0, "is_favorite", False))

            view = CollectionFlipView(
                session_factory=self.bot.async_session_factory,
                owner_id=owner_id,
                viewer_id=viewer_id,
                instance_ids=ids,
                first_card_id=first_card_id,
                first_wishlisted=first_wishlisted,
                first_favorited=first_favorited,
                show_favorite=(owner_id == viewer_id),
            )
            await _send_collection_card(
                ctx,
                embed=embed,
                public_id=inst0.public_id,
                view=view,
                files=slab_files,
                content=_collection_web_footer().lstrip(),
                ephemeral=ephe,
            )
        except SQLAlchemyError:
            _LOG.exception("colv database error for %s", ctx.author.id)
            await ctx.send("Could not load that collection. Try again.", ephemeral=ephe)
        except Exception:
            _LOG.exception("colv failed for %s", ctx.author.id)
            await reply_command_failure(ctx)

    @commands.hybrid_command(
        name="coll",
        aliases=[pp_alias("coll")],
        description="Collection text list (◀▶) — chat: ppcoll",
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
                    .where(
                        UserCardInstance.discord_user_id == owner_id,
                        user_instance_not_in_active_auction(),
                    )
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
                await ctx.send(f"<@{owner_id}> has an empty collection. **`ppcd`**", ephemeral=ephe)
            else:
                await ctx.send("You have an empty collection. **`ppcd`**", ephemeral=ephe)
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
        aliases=[*pp_chat_aliases("cevolve", "evol", "ev")],
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
                blocked = await collection_sell_block_reason(
                    session, discord_user_id=uid, instance_id=inst.id,
                )
                if blocked:
                    await ctx.send(blocked, ephemeral=ephe)
                    return
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
        aliases=[*pp_chat_aliases("drop_boost", "dboost", "db")],
        description="Half drop cooldown — buy once on the website shop (or legacy Discord SKU)",
    )
    async def drop_boost_shop(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        base = self._settings.drop_cooldown_base_seconds
        prem = self._settings.drop_cooldown_premium_seconds
        stripe_price = (self._settings.stripe_price_ids or {}).get("half_drop_cooldown")
        if stripe_price:
            from poke_pon_bot.web.frontend_urls import shop_page_url

            await ctx.send(
                "**Half drop cooldown** — one-time purchase: after checkout, your **`/cd`** / **`ppcd`** wait drops from "
                f"{_fmt_cd_sentence(base)} to {_fmt_cd_sentence(prem)} permanently.\n"
                f"Buy on the website: **{shop_page_url(self._settings)}** (Perks section).",
                ephemeral=True,
            )
            return
        sku = self._settings.discord_drop_boost_sku_id
        if sku is None:
            await ctx.send(
                "Drop boost isn’t configured. Add **STRIPE_PRICE_HALF_DROP_COOLDOWN** on the bot "
                "or **DISCORD_DROP_BOOST_SKU_ID** for the legacy Discord SKU.",
                ephemeral=True,
            )
            return
        try:
            await ctx.send(
                "**Half drop cooldown** — one-time purchase: after checkout, your **`/cd`** / **`ppcd`** wait drops from "
                f"{_fmt_cd_sentence(base)} to {_fmt_cd_sentence(prem)}.\n"
                "Tap **Buy** below to open Discord’s purchase flow (legacy).",
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

    async def _offer_cd_reroll(self, ctx: commands.Context, view: PackPickView) -> None:
        """Ephemeral reroll only when the claim grid is full (25 slots) and inline button did not fit."""
        if view._reroll_used or not view._cards:
            return
        if _inline_reroll_button_row(len(view._cards)) is not None:
            return
        open_slots = [i for i in range(len(view._cards)) if i not in view._slot_pid]
        if not open_slots:
            return
        from poke_pon_bot.services.crystal_sinks import CD_REROLL_CRYSTAL_COST
        from poke_pon_bot.services.crystals import format_crystals

        reroll_view = IssuerCdRerollView(pack_view=view)
        hint = (
            f"Optional: tap **Reroll {format_crystals(CD_REROLL_CRYSTAL_COST)}** to replace one random "
            "unclaimed card (same odds as this pack)."
        )
        try:
            if ctx.interaction is not None:
                if ctx.interaction.response.is_done():
                    await ctx.interaction.followup.send(
                        hint, view=reroll_view, ephemeral=True
                    )
                else:
                    await ctx.interaction.response.send_message(
                        hint, view=reroll_view, ephemeral=True
                    )
            else:
                await ctx.send(hint, view=reroll_view, ephemeral=True)
        except (discord.HTTPException, discord.NotFound):
            _LOG.debug("Could not send cd reroll offer", exc_info=True)

    async def run_card_drop(
        self,
        ctx: commands.Context,
        *,
        is_private: bool,
        skip_cooldown: bool = False,
        card_count: int | None = None,
        luck_percent: float = 0.0,
        apply_drop_accounting: bool = True,
        content_header: str = "",
        max_grabs_per_user: int = 1,
        claim_seconds: int | None = None,
    ) -> None:
        """Shared ``/cd`` pack flow (collage, claim buttons, missions, wishlist pings)."""
        uid = ctx.author.id
        # Discord slash/components must be acknowledged within ~3s. Cooldown is a
        # fast read; defer before writes, referrals, and pack roll.
        if apply_drop_accounting and not skip_cooldown:
            per = await self._effective_drop_cooldown_seconds(ctx, uid)
            now = time.time()
            async with self.bot.async_session_factory() as session:
                last_dt = await self._wallet.get_last_drop_at(session, uid)
                last = last_dt.timestamp() if last_dt is not None else 0.0
                if last and now - last < per:
                    msg = _drop_cooldown_message(per - (now - last), per_seconds=per)
                    if ctx.interaction is not None and not ctx.interaction.response.is_done():
                        await ctx.interaction.response.send_message(
                            msg, ephemeral=_hybrid_ephemeral(ctx)
                        )
                    else:
                        await ctx.send(msg, ephemeral=_hybrid_ephemeral(ctx))
                    return

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=is_private)

        if apply_drop_accounting and not skip_cooldown:
            async with self.bot.async_session_factory() as session:
                await self._wallet.record_drop(session, uid)
                await clear_drop_reminder_after_drop(session, uid)
                drop_use_notices = await MissionService().record_drop_use(session, uid)
                await session.commit()

            schedule_mission_completion_dms(
                self.bot, user_id=uid, notices=drop_use_notices
            )

            referral_result = await record_referral_cd_use(
                self.bot.async_session_factory, uid
            )
            if referral_result is not None:
                if referral_result.first_pack is not None:
                    await notify_referral_first_pack(self.bot, referral_result.first_pack)
                if referral_result.threshold is not None:
                    await notify_referral_reward(self.bot, referral_result.threshold)

        drop = DropService()
        guild_id = ctx.guild.id if ctx.guild is not None else None
        theme_line = ""
        luck_line = ""
        set_chase_line = ""
        pack: list = []
        chase_slots: frozenset[int] = frozenset()
        chase_season = None
        try:
            async with self.bot.async_session_factory() as session:
                theme = await resolve_active_cd_drop_theme(session, guild_id)
                theme_line = format_theme_status_line(theme)
                from poke_pon_bot.services.set_chase import build_status, format_set_chase_cd_header

                chase_status = await build_status(session)
                chase_season = chase_status.season if chase_status is not None else None
                luck_row = None
                if guild_id is not None:
                    luck_row = await get_luck_boost_row(session, guild_id=guild_id)
                if luck_row is None:
                    luck_row = await get_luck_boost_row(session, guild_id=None)
                scope = (
                    f"server {guild_id}"
                    if luck_row is not None and luck_row.guild_id is not None
                    else "global"
                )
                luck_pct = await resolve_active_rarity_luck_boost(session, guild_id)
                luck_line = format_luck_boost_line(luck_pct, scope_label=scope)
                draws = await drop.roll_pack(
                    session,
                    drop_table_code="default",
                    card_count=card_count,
                    luck_percent=luck_percent,
                    guild_id=guild_id,
                )
                pack = [d.card for d in draws]
                chase_slots = frozenset(
                    i for i, d in enumerate(draws) if d.from_set_chase
                )
                if chase_season is not None and chase_slots:
                    set_chase_line = format_set_chase_cd_header(
                        chase_season,
                        chase_slot_indices=sorted(chase_slots),
                        cards=pack,
                    )
        except RuntimeError as exc:
            await ctx.send(str(exc), ephemeral=False)
            return
        except LookupError as exc:
            await ctx.send(str(exc), ephemeral=False)
            return
        except ValueError as exc:
            await ctx.send(str(exc), ephemeral=False)
            return
        except SQLAlchemyError:
            _LOG.exception("run_card_drop failed user=%s", uid)
            await ctx.send(
                "Could not open your pack right now — try again in a moment.",
                ephemeral=_hybrid_ephemeral(ctx),
            )
            return

        is_slash = ctx.interaction is not None
        private_reply = is_slash and is_private
        claim_secs = _PACK_CLAIM_SECONDS if claim_seconds is None else max(30, int(claim_seconds))
        deadline_unix = int(time.time()) + claim_secs
        mission_block = ""
        try:
            async with self.bot.async_session_factory() as session:
                alerts = await MissionService().find_pack_mission_alerts(session, pack)
            mission_block = _pack_mission_block(
                alerts,
                guild=ctx.guild,
                private_pack=private_reply,
                viewer_id=uid,
            )
        except SQLAlchemyError:
            _LOG.exception("pack mission alerts for drop user=%s", uid)
        view = PackPickView(
            session_factory=self.bot.async_session_factory,
            issuer_id=ctx.author.id,
            opener_mention=ctx.author.mention,
            cards=pack,
            deadline_unix=deadline_unix,
            private_pack=private_reply,
            mission_block=mission_block,
            max_grabs_per_user=max_grabs_per_user,
            claim_seconds=claim_secs,
            guild_id=guild_id,
            chase_slot_indices=chase_slots if chase_slots else None,
        )
        try:
            png = await render_pack_collage_png(pack)
        except (OSError, ValueError, httpx.HTTPError):
            png = None
        content = view.build_content()
        header_parts = [
            p
            for p in (
                set_chase_line.strip(),
                theme_line.strip(),
                luck_line.strip(),
                content_header.strip(),
            )
            if p
        ]
        if header_parts:
            content = "\n\n".join(header_parts) + f"\n\n{content}"
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
            msg = await ctx.send(
                content=content + "\n\n*(Could not build card collage.)*",
                ephemeral=private_reply,
                view=view,
            )
            view.message = msg

        await self._offer_cd_reroll(ctx, view)

        if guild_id is not None:
            try:
                from poke_pon_bot.services.guild_milestones import (
                    announce_milestone_tiers,
                    record_pack_opened,
                )

                async with self.bot.async_session_factory() as session:
                    crossed = await record_pack_opened(
                        session,
                        guild_id=int(guild_id),
                        discord_user_id=int(uid),
                    )
                    await session.commit()
                if crossed:
                    asyncio.create_task(
                        announce_milestone_tiers(
                            self.bot,
                            guild_id=int(guild_id),
                            crossed_tiers=crossed,
                        )
                    )
            except SQLAlchemyError:
                _LOG.debug("guild pack milestone increment failed", exc_info=True)

        if not private_reply and msg.channel is not None:
            prefix = "\n\n".join(header_parts) if header_parts else ""
            view.bind_drop_host(
                self,
                channel_id=int(msg.channel.id),
                guild_id=guild_id,
                content_prefix=prefix,
            )
            self.register_drop_view(view, message_id=int(msg.id))
            await self._persist_drop_view(view)

    @commands.hybrid_command(
        name="cd",
        aliases=[pp_alias("cd")],
        description="Card drop (open a pack) — chat: ppcd",
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
        await self.run_card_drop(ctx, is_private=private == "yes")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GachaCog(bot))
    _LOG.info(
        "Loaded gacha cog: `ppcd`, `/drop_boost`, `/cd`, `/cs`, `/cv`, `/colv`, `/coll`, `/cevolve`."
    )

