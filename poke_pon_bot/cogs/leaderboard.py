"""Leaderboard command: server or global rankings by card stats and auction sales."""

from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands
from poke_pon_bot.services.grading import format_grade_slab_badge
from poke_pon_bot.services.guild_milestones import GUILD_STAT_CATEGORIES
from poke_pon_bot.services.leaderboard import (
    LEADERBOARD_TITLES,
    SERVER_LEADERBOARD_CATEGORIES,
    fetch_leaderboard,
)
from poke_pon_bot.services.wallet import format_pokedollars

_LOG = logging.getLogger(__name__)

_PER_PAGE = 10
_MAX_PAGES = 50
_MAX_ENTRIES = _PER_PAGE * _MAX_PAGES


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
# Embed builder (generic, page-aware)
# ---------------------------------------------------------------------------


def _format_entry(category: str, rank: int, entry: tuple) -> str:
    if category == "strongest":
        uid, name, dmg, grade = entry
        return (
            f"**{rank}.** {_mention(uid)} — **{_truncate(name, 28)}** · ⚡ {dmg} damage"
            f"{format_grade_slab_badge(grade)}"
        )
    if category == "tankiest":
        uid, name, hp, grade = entry
        return (
            f"**{rank}.** {_mention(uid)} — **{_truncate(name, 28)}** · ❤ {hp} HP"
            f"{format_grade_slab_badge(grade)}"
        )
    if category == "rarest":
        uid, name, rarity_name, _so, grade = entry
        return (
            f"**{rank}.** {_mention(uid)} — **{_truncate(name, 28)}** · ✦ {rarity_name}"
            f"{format_grade_slab_badge(grade)}"
        )
    if category == "auction":
        uid, name, price, grade = entry
        return (
            f"**{rank}.** **{_truncate(name, 28)}** · sold for **{format_pokedollars(price)}**"
            f"{format_grade_slab_badge(grade)} — by {_mention(uid)}"
        )
    if category == "graded":
        uid, name, grade_val, grade_lbl = entry
        return (
            f"**{rank}.** {_mention(uid)} — **{_truncate(name, 28)}** · "
            f"🏆 **{grade_val}** {grade_lbl}"
        )
    if category == "packs":
        uid, count = entry
        return f"**{rank}.** {_mention(uid)} — **{int(count):,}** packs opened"
    if category == "traders":
        uid, count = entry
        return f"**{rank}.** {_mention(uid)} — **{int(count):,}** trades completed"
    if category == "collectors":
        uid, count = entry
        return f"**{rank}.** {_mention(uid)} — **{int(count):,}** unique cards"
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

    title = f"Leaderboard — {LEADERBOARD_TITLES.get(category, category)} ({scope_label})"
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
        description="View server or global leaderboards (strongest, tankiest, rarest, graded, top auctions)",
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
            app_commands.Choice(name="Top graded slabs", value="graded"),
            app_commands.Choice(name="Packs opened (server)", value="packs"),
            app_commands.Choice(name="Top traders (server)", value="traders"),
            app_commands.Choice(name="Top collectors (server)", value="collectors"),
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

        if category in GUILD_STAT_CATEGORIES and not is_server:
            await ctx.send(
                "**Packs opened**, **Top traders**, and **Top collectors** require "
                "**Server** scope inside a Discord server.",
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
                if category not in SERVER_LEADERBOARD_CATEGORIES:
                    await ctx.send("Unknown category.", ephemeral=False)
                    return
                entries = await fetch_leaderboard(
                    session,
                    category,
                    member_ids=member_ids,
                    guild_id=(
                        guild_id
                        if is_server and (category == "auction" or category in GUILD_STAT_CATEGORIES)
                        else None
                    ),
                )
        except Exception:
            _LOG.exception("leaderboard query failed: category=%s scope=%s", category, scope)
            await ctx.send("Could not load the leaderboard. Try again.", ephemeral=False)
            return

        if not entries:
            if category == "auction":
                empty_msg = "_No auction sales yet._"
            elif category == "graded":
                empty_msg = "_No graded slabs yet._"
            else:
                empty_msg = "_No ranked players yet._"
            title = f"Leaderboard — {LEADERBOARD_TITLES.get(category, category)} ({scope_label})"
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
