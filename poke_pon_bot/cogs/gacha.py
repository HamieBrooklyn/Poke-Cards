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

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.collection_search import any_filter_set, search_collection
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.services.pack_collage import render_pack_collage_png

_LOG = logging.getLogger(__name__)


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _fmt_obtained(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return discord.utils.format_dt(dt, "R")


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

    e.set_footer(text=f"Inventory copy #{inst.id} · ID `{card.tcg_card_id}`")
    return e


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

                await drop.claim_card(
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


class GachaCog(commands.Cog):
    """Weighted drops using the imported Pokémon TCG catalog."""

    collection = app_commands.Group(
        name="collection",
        description="View and search cards you have saved from drops",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


    @collection.command(name="recent", description="Show your most recently saved cards")
    @app_commands.describe(limit="How many recent cards to list")
    async def collection_recent(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.bot.async_session_factory() as session:
            stmt = (
                select(UserCardInstance, Card)
                .join(Card, UserCardInstance.card_id == Card.id)
                .where(UserCardInstance.discord_user_id == interaction.user.id)
                .order_by(UserCardInstance.obtained_at.desc())
                .limit(int(limit))
            )
            rows = (await session.execute(stmt)).all()

        if not rows:
            await interaction.followup.send(
                "No cards yet — complete `/drop` and choose a card to keep after syncing the catalog.",
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for inst, card in rows:
            r = card.tcg_rarity or "?"
            lines.append(
                f"· **{card.name}** — {card.set_name} #{card.collector_number} ({r}) · {_fmt_obtained(inst.obtained_at)}",
            )

        body = "\n".join(lines)
        await interaction.followup.send(
            f"**Recent pulls** (newest first)\n{body}",
            ephemeral=True,
        )

    @collection.command(name="search", description="Find cards in your collection by name, rarity, Pokédex #, or rank")
    @app_commands.describe(
        name="Match card name (substring, case-insensitive)",
        rarity='Match printed rarity text (e.g. "illustration", "double")',
        pokedex="National dex number listed on the card data",
        slot=(
            "Pick the nth matching card — **1** = newest among matches "
            "(leave blank to list up to `limit` matches)"
        ),
        limit="How many rows to show when browsing (ignored when `slot` is set)",
    )
    async def collection_search(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        rarity: str | None = None,
        pokedex: int | None = None,
        slot: app_commands.Range[int, 1, 500] | None = None,
        limit: app_commands.Range[int, 1, 25] = 15,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not any_filter_set(
            name_contains=name,
            rarity_contains=rarity,
            pokedex=pokedex,
            slot=slot,
        ):
            await interaction.followup.send(
                "Provide at least one filter: **`name`**, **`rarity`**, **`pokedex`**, or **`slot`**.",
                ephemeral=True,
            )
            return

        async with self.bot.async_session_factory() as session:
            rows, total = await search_collection(
                session,
                discord_user_id=interaction.user.id,
                name_contains=name,
                rarity_contains=rarity,
                pokedex=pokedex,
                slot=slot,
                page_limit=int(limit),
            )

        if total == 0:
            await interaction.followup.send(
                "No cards matched those filters.",
                ephemeral=True,
            )
            return

        if slot is not None and slot > total:
            await interaction.followup.send(
                f"You only have **{total}** matching card(s); **`slot`** must be between **1** and **{total}** "
                "(1 = newest among matches).",
                ephemeral=True,
            )
            return

        if not rows:
            await interaction.followup.send(
                "No rows returned — try loosening your filters.",
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for rank, (inst, card) in enumerate(rows, start=1):
            dex_bit = ""
            if card.dex_numbers:
                dex_bit = f" · dex {', '.join(str(d) for d in card.dex_numbers[:4])}"
                if len(card.dex_numbers) > 4:
                    dex_bit += "…"
            r = card.tcg_rarity or "?"
            lines.append(
                f"`{rank}.` **{card.name}** — {card.set_name} #{card.collector_number} · "
                f"*{r}*{dex_bit} · {_fmt_obtained(inst.obtained_at)}",
            )

        header = (
            f"**Matches** — showing **{len(rows)}** of **{total}** "
            f"(newest first; use **`slot`** to jump to a single rank)\n"
        )
        if slot is None and total > len(rows):
            header += f"_…and **{total - len(rows)}** more — narrow with **`name`** / **`rarity`** / **`pokedex`** or raise **`limit`**._\n"

        await interaction.followup.send(
            _truncate(header + "\n".join(lines), 2000),
            ephemeral=True,
        )

    async def _collection_view_execute(
        self,
        interaction: discord.Interaction,
        *,
        slot: int | None,
        name: str | None,
        rarity: str | None,
        pokedex: int | None,
    ) -> None:
        """Body for `/collection show` and `/view_collection` (caller must defer first)."""
        if not any_filter_set(
            name_contains=name,
            rarity_contains=rarity,
            pokedex=pokedex,
            slot=slot,
        ):
            await interaction.followup.send(
                "Use **`slot`** (e.g. **1** for your newest card overall), and/or filters "
                "**`name`** / **`rarity`** / **`pokedex`** to pick which copy to open.",
                ephemeral=True,
            )
            return

        async with self.bot.async_session_factory() as session:
            if slot is not None:
                rows, total = await search_collection(
                    session,
                    discord_user_id=interaction.user.id,
                    name_contains=name,
                    rarity_contains=rarity,
                    pokedex=pokedex,
                    slot=slot,
                    page_limit=1,
                )
            else:
                rows, total = await search_collection(
                    session,
                    discord_user_id=interaction.user.id,
                    name_contains=name,
                    rarity_contains=rarity,
                    pokedex=pokedex,
                    slot=None,
                    page_limit=2,
                )

        if total == 0:
            await interaction.followup.send(
                "No cards matched — try different filters.",
                ephemeral=True,
            )
            return

        if slot is not None:
            if slot > total:
                await interaction.followup.send(
                    f"You only have **{total}** matching card(s); **`slot`** must be **1–{total}**.",
                    ephemeral=True,
                )
                return
            if not rows:
                await interaction.followup.send(
                    "Could not load that slot.",
                    ephemeral=True,
                )
                return
            inst, card = rows[0]
            note = (
                f"Showing match **#{slot}** of **{total}** (newest first)."
                if total > 1
                else None
            )
            embed = _collection_view_embed(inst, card, rank_note=note)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if total > 1:
            await interaction.followup.send(
                f"You have **{total}** cards matching those filters. Add **`slot`** "
                f"(**1**–**{total}**, **1** = newest) or narrow **`name`** / **`rarity`** / **`pokedex`**. "
                "Use `/collection search` with the same filters to preview the list.",
                ephemeral=True,
            )
            return

        if not rows:
            await interaction.followup.send(
                "Could not load that card.",
                ephemeral=True,
            )
            return

        inst, card = rows[0]
        embed = _collection_view_embed(
            inst,
            card,
            rank_note="Only one card matched your filters.",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @collection.command(
        name="show",
        description="Inspect one card from your collection (image + details)",
    )
    @app_commands.describe(
        slot=(
            "Which match to show — **1** = newest among matches "
            "(same order as `/collection search`). Omit if only one card matches."
        ),
        name="Filter by card name (substring)",
        rarity="Filter by printed rarity text (substring)",
        pokedex="Filter by national Pokédex number on the card",
    )
    async def collection_show(
        self,
        interaction: discord.Interaction,
        slot: app_commands.Range[int, 1, 500] | None = None,
        name: str | None = None,
        rarity: str | None = None,
        pokedex: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._collection_view_execute(
            interaction,
            slot=slot,
            name=name,
            rarity=rarity,
            pokedex=pokedex,
        )

    @app_commands.command(
        name="view_collection",
        description="View one card from your collection — large image & details (same as /collection show)",
    )
    @app_commands.describe(
        slot=(
            "Which match to show — **1** = newest among matches. "
            "Same ordering as `/collection search`."
        ),
        name="Filter by card name (substring)",
        rarity="Filter by printed rarity text (substring)",
        pokedex="Filter by national Pokédex number on the card",
    )
    async def view_collection_cmd(
        self,
        interaction: discord.Interaction,
        slot: app_commands.Range[int, 1, 500] | None = None,
        name: str | None = None,
        rarity: str | None = None,
        pokedex: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._collection_view_execute(
            interaction,
            slot=slot,
            name=name,
            rarity=rarity,
            pokedex=pokedex,
        )

    @app_commands.command(
        name="drop",
        description="Open a pack — pick one revealed card to keep",
    )
    @app_commands.describe(private="Only you see the pack")
    async def drop_cmd(
        self,
        interaction: discord.Interaction,
        private: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=private)
        drop = DropService()
        async with self.bot.async_session_factory() as session:
            try:
                pack = await drop.roll_pack(session, drop_table_code="default")
            except RuntimeError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except LookupError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return

        header = (
            "**Pack opened** — always **2** cards; extra slots may appear "
            "(rarer each time). Tap **one** button below to save that card."
        )

        slots_text = "\n".join(
            f"**#{i}** {c.name} — *{c.tcg_rarity or '?'}* · {c.set_name} #{card.collector_number}"
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
            issuer_id=interaction.user.id,
            cards=pack,
        )

        if png is not None:
            file = discord.File(png, filename="pack.png")
            embed.set_image(url="attachment://pack.png")
            await interaction.followup.send(
                content=header,
                embed=embed,
                file=file,
                ephemeral=private,
                view=view,
            )
        else:
            await interaction.followup.send(
                content=header + "\n*(Could not build card collage.)*",
                embed=embed,
                ephemeral=private,
                view=view,
            )


async def setup(bot: commands.Bot) -> None:
    cog = GachaCog(bot)
    await bot.add_cog(cog)
    names = [c.name for c in cog.collection.walk_commands()]
    _LOG.info("Registered /collection subcommands: %s", ", ".join(names))

