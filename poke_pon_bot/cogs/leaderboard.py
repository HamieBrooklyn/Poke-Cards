"""Leaderboard command: server or global rankings by card stats and auction sales."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import Integer, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.auction import AUCTION_STATUS_ENDED_SOLD, CardAuction
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.wallet import format_pokedollars

_LOG = logging.getLogger(__name__)

_PER_PAGE = 10
_MAX_PAGES = 50
_MAX_ENTRIES = _PER_PAGE * _MAX_PAGES
_FETCH_LIMIT = 50_000


def _max_attack_damage(attacks: Any) -> int:
    if not isinstance(attacks, list):
        return 0
    best = 0
    for atk in attacks:
        if not isinstance(atk, dict):
            continue
        raw = atk.get("damage")
        if raw is None:
            continue
        digits: list[str] = []
        for ch in str(raw):
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            best = max(best, int("".join(digits)))
    return best


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _mention(uid: int) -> str:
    return f"<@{uid}>"


def _guild_member_ids(guild: discord.Guild | None) -> set[int] | None:
    """Use the cached member list. Requires Members intent."""
    if guild is None:
        return None
    ids: set[int] = {m.id for m in guild.members if not m.bot}
    return ids or None


# ---------------------------------------------------------------------------
# Query functions — return full ranked lists (capped at _MAX_ENTRIES)
# ---------------------------------------------------------------------------

async def _leaderboard_strongest(
    session: AsyncSession,
    member_ids: set[int] | None,
) -> list[tuple[int, str, int]]:
    stmt = (
        select(UserCardInstance.discord_user_id, Card.name, Card.attacks)
        .join(Card, Card.id == UserCardInstance.card_id)
        .where(Card.attacks.isnot(None))
    )
    if member_ids is not None:
        stmt = stmt.where(UserCardInstance.discord_user_id.in_(member_ids))
    rows = (await session.execute(stmt.limit(_FETCH_LIMIT))).all()

    best: dict[int, tuple[str, int]] = {}
    for uid, name, attacks in rows:
        dmg = _max_attack_damage(attacks)
        if dmg <= 0:
            continue
        prev = best.get(uid)
        if prev is None or dmg > prev[1]:
            best[uid] = (name, dmg)

    ranked = sorted(best.items(), key=lambda x: x[1][1], reverse=True)[:_MAX_ENTRIES]
    return [(uid, name, dmg) for uid, (name, dmg) in ranked]


async def _leaderboard_tankiest(
    session: AsyncSession,
    member_ids: set[int] | None,
) -> list[tuple[int, str, int]]:
    hp_int = func.cast(func.coalesce(func.nullif(Card.hp, ""), "0"), Integer())
    stmt = (
        select(UserCardInstance.discord_user_id, Card.name, hp_int.label("hp_val"))
        .join(Card, Card.id == UserCardInstance.card_id)
        .where(Card.hp.isnot(None), Card.hp != "")
        .order_by(desc("hp_val"))
    )
    if member_ids is not None:
        stmt = stmt.where(UserCardInstance.discord_user_id.in_(member_ids))
    rows = (await session.execute(stmt.limit(_FETCH_LIMIT))).all()

    best: dict[int, tuple[str, int]] = {}
    for uid, name, hp_val in rows:
        hp = int(hp_val) if hp_val else 0
        if hp <= 0:
            continue
        prev = best.get(uid)
        if prev is None or hp > prev[1]:
            best[uid] = (name, hp)

    ranked = sorted(best.items(), key=lambda x: x[1][1], reverse=True)[:_MAX_ENTRIES]
    return [(uid, name, hp) for uid, (name, hp) in ranked]


async def _leaderboard_rarest(
    session: AsyncSession,
    member_ids: set[int] | None,
) -> list[tuple[int, str, str, int]]:
    stmt = (
        select(
            UserCardInstance.discord_user_id,
            Card.name,
            RarityClass.display_name,
            RarityClass.sort_order,
        )
        .join(Card, Card.id == UserCardInstance.card_id)
        .join(RarityClass, RarityClass.id == Card.rarity_class_id)
        .order_by(desc(RarityClass.sort_order), desc(UserCardInstance.obtained_at))
    )
    if member_ids is not None:
        stmt = stmt.where(UserCardInstance.discord_user_id.in_(member_ids))
    rows = (await session.execute(stmt.limit(_FETCH_LIMIT))).all()

    best: dict[int, tuple[str, str, int]] = {}
    for uid, name, rarity_name, sort_order in rows:
        order = int(sort_order) if sort_order else 0
        prev = best.get(uid)
        if prev is None or order > prev[2]:
            best[uid] = (name, rarity_name or "Unknown", order)

    ranked = sorted(best.items(), key=lambda x: x[1][2], reverse=True)[:_MAX_ENTRIES]
    return [(uid, name, rn, so) for uid, (name, rn, so) in ranked]


async def _leaderboard_auction(
    session: AsyncSession,
    member_ids: set[int] | None,
    guild_id: int | None,
) -> list[tuple[int, str, int]]:
    stmt = (
        select(
            CardAuction.seller_discord_id,
            Card.name,
            CardAuction.high_bid_pokedollars,
        )
        .join(UserCardInstance, UserCardInstance.id == CardAuction.instance_id)
        .join(Card, Card.id == UserCardInstance.card_id)
        .where(CardAuction.status == AUCTION_STATUS_ENDED_SOLD)
        .order_by(desc(CardAuction.high_bid_pokedollars))
    )
    if member_ids is not None:
        stmt = stmt.where(CardAuction.seller_discord_id.in_(member_ids))
    if guild_id is not None:
        stmt = stmt.where(CardAuction.guild_id == guild_id)

    rows = (await session.execute(stmt.limit(_MAX_ENTRIES))).all()
    return [(uid, name, int(price)) for uid, name, price in rows]


# ---------------------------------------------------------------------------
# Embed builder (generic, page-aware)
# ---------------------------------------------------------------------------

_TITLES = {
    "strongest": "Strongest Cards",
    "tankiest": "Tankiest Cards",
    "rarest": "Rarest Cards",
    "auction": "Top Auction Sales",
}


def _format_entry(category: str, rank: int, entry: tuple) -> str:
    if category == "strongest":
        uid, name, dmg = entry
        return f"**{rank}.** {_mention(uid)} — **{_truncate(name, 28)}** · ⚡ {dmg} damage"
    elif category == "tankiest":
        uid, name, hp = entry
        return f"**{rank}.** {_mention(uid)} — **{_truncate(name, 28)}** · ❤ {hp} HP"
    elif category == "rarest":
        uid, name, rarity_name, _so = entry
        return f"**{rank}.** {_mention(uid)} — **{_truncate(name, 28)}** · ✦ {rarity_name}"
    elif category == "auction":
        uid, name, price = entry
        return f"**{rank}.** **{_truncate(name, 28)}** · sold for **{format_pokedollars(price)}** — by {_mention(uid)}"
    return ""


def _build_page_embed(
    category: str,
    scope_label: str,
    entries: list[tuple],
    page: int,
    total_pages: int,
    invoker_id: int,
) -> discord.Embed:
    start = page * _PER_PAGE
    end = min(start + _PER_PAGE, len(entries))
    page_entries = entries[start:end]

    lines: list[str] = []
    for i, entry in enumerate(page_entries):
        rank = start + i + 1
        lines.append(_format_entry(category, rank, entry))

    empty_msg = "_No auction sales yet._" if category == "auction" else "_No data yet._"
    body = "\n".join(lines) or empty_msg

    title = f"Leaderboard — {_TITLES.get(category, category)} ({scope_label})"
    embed = discord.Embed(title=title, description=body)

    invoker_rank: int | None = None
    uid_index = 0
    for i, entry in enumerate(entries):
        if entry[uid_index] == invoker_id:
            invoker_rank = i + 1
            break

    footer_parts: list[str] = []
    if total_pages > 1:
        footer_parts.append(f"Page {page + 1} of {total_pages}")
    if invoker_rank is not None:
        footer_parts.append(f"Your rank: #{invoker_rank}")
    if footer_parts:
        embed.set_footer(text=" · ".join(footer_parts))

    return embed


# ---------------------------------------------------------------------------
# Pagination view
# ---------------------------------------------------------------------------

class LeaderboardView(discord.ui.View):
    def __init__(
        self,
        *,
        category: str,
        scope_label: str,
        entries: list[tuple],
        invoker_id: int,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=300.0)
        self.category = category
        self.scope_label = scope_label
        self.entries = entries
        self.invoker_id = invoker_id
        self.page = page
        self.total_pages = max(1, min(_MAX_PAGES, -(-len(entries) // _PER_PAGE)))
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def _embed(self) -> discord.Embed:
        return _build_page_embed(
            self.category,
            self.scope_label,
            self.entries,
            self.page,
            self.total_pages,
            self.invoker_id,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Use your own `/leaderboard` command.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True  # type: ignore[union-attr]
        msg = getattr(self, "message", None)
        if msg:
            try:
                await msg.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class LeaderboardCog(commands.Cog):
    """Server and global leaderboards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="leaderboard",
        aliases=["pcleaderboard", "lb"],
        description="View server or global leaderboards (strongest, tankiest, rarest, top auctions)",
    )
    @app_commands.describe(
        category="What to rank by",
        scope="Server members only, or all players globally",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Strongest cards", value="strongest"),
            app_commands.Choice(name="Tankiest cards", value="tankiest"),
            app_commands.Choice(name="Rarest cards", value="rarest"),
            app_commands.Choice(name="Top auction sales", value="auction"),
        ],
        scope=[
            app_commands.Choice(name="Server", value="server"),
            app_commands.Choice(name="Global", value="global"),
        ],
    )
    async def leaderboard_cmd(
        self,
        ctx: commands.Context,
        category: str = "strongest",
        scope: str = "server",
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)

        uid = ctx.author.id
        is_server = scope == "server"

        if is_server and ctx.guild is None:
            await ctx.send(
                "Server leaderboard is only available inside a server. "
                "Use **`/leaderboard category scope:Global`** here.",
                ephemeral=False,
            )
            return

        member_ids: set[int] | None = None
        guild_id: int | None = None
        scope_label = "Global"

        if is_server and ctx.guild is not None:
            member_ids = _guild_member_ids(ctx.guild)
            guild_id = ctx.guild.id
            scope_label = "Server"
            if not member_ids:
                await ctx.send(
                    "Could not read server members. Make sure the bot has the **Server Members Intent** enabled.",
                    ephemeral=False,
                )
                return

        try:
            async with self.bot.async_session_factory() as session:
                if category == "strongest":
                    entries = await _leaderboard_strongest(session, member_ids)
                elif category == "tankiest":
                    entries = await _leaderboard_tankiest(session, member_ids)
                elif category == "rarest":
                    entries = await _leaderboard_rarest(session, member_ids)
                elif category == "auction":
                    entries = await _leaderboard_auction(session, member_ids, guild_id if is_server else None)
                else:
                    await ctx.send("Unknown category.", ephemeral=False)
                    return
        except Exception:
            _LOG.exception("leaderboard query failed: category=%s scope=%s", category, scope)
            await ctx.send("Could not load the leaderboard. Try again.", ephemeral=False)
            return

        if not entries:
            empty_msg = "_No auction sales yet._" if category == "auction" else "_No ranked players yet._"
            title = f"Leaderboard — {_TITLES.get(category, category)} ({scope_label})"
            embed = discord.Embed(title=title, description=empty_msg)
            await ctx.send(embed=embed, ephemeral=False)
            return

        view = LeaderboardView(
            category=category,
            scope_label=scope_label,
            entries=entries,
            invoker_id=uid,
        )
        msg = await ctx.send(embed=view._embed(), view=view, ephemeral=False)
        view.message = msg


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LeaderboardCog(bot))
