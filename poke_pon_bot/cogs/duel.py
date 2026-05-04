"""Pokémon card duels: bet, combat deck, turn-based combat from catalog stats."""

from __future__ import annotations

import logging
import random
import secrets
from dataclasses import dataclass
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.instance_public_id import compact_public_id_for_line
from poke_pon_bot.services.combat_deck import (
    MAX_DECK,
    MIN_DECK,
    get_saved_instance_ids,
    load_fighters_ordered,
    set_deck_from_public_ids,
)
from poke_pon_bot.services.duel_engine import DuelRuntime, Fighter
from poke_pon_bot.services.wallet import InsufficientPokedollarsError, WalletService, format_pokedollars

_LOG = logging.getLogger(__name__)


def _hybrid_ephemeral(ctx: commands.Context) -> bool:
    return False


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


@dataclass
class DuelLobby:
    lobby_id: str
    challenger_id: int
    opponent_id: int
    bet: int
    channel_id: int
    message_id: int | None = None
    challenger_ready: bool = False
    opponent_ready: bool = False


def _invite_embed(lobby: DuelLobby, opponent: discord.Member) -> discord.Embed:
    stake = (
        "Friendly duel (**no** stake)."
        if lobby.bet <= 0
        else f"Stake: **{format_pokedollars(lobby.bet)}** each (**{format_pokedollars(lobby.bet * 2)}** pot)."
    )
    return discord.Embed(
        title="Duel challenge",
        description=(
            f"{stake}\n\n"
            f"<@{lobby.challenger_id}> challenges {opponent.mention}!\n"
            f"**Opponent:** use **Accept** or **Decline**.\n"
            f"**Challenger:** **Cancel challenge** to withdraw."
        ),
    )


def _ready_embed(lobby: DuelLobby) -> discord.Embed:
    stake = (
        "No stake."
        if lobby.bet <= 0
        else f"Stake locked in at **{format_pokedollars(lobby.bet)}** each."
    )
    cr = "✅" if lobby.challenger_ready else "⏳"
    or_ = "✅" if lobby.opponent_ready else "⏳"
    return discord.Embed(
        title="Duel — lock in",
        description=(
            f"{stake}\n\n"
            "Decks stay **hidden** until the first turn.\n"
            f"Set your bench with **`/deck set`** (or **`c deck set`** …), then press **Ready**.\n\n"
            f"{cr} Challenger ready\n"
            f"{or_} Opponent ready"
        ),
    )


def _combat_embed(combat: DuelRuntime) -> discord.Embed | None:
    if not combat.challenger_lineup or not combat.opponent_lineup:
        return None
    ca = combat.challenger_lineup[0]
    oa = combat.opponent_lineup[0]
    log = "\n\n".join(combat.log_lines[-6:])
    e = discord.Embed(
        title="Duel — battle",
        description=_truncate(log, 3500),
    )
    e.add_field(
        name="Challenger (active)",
        value=f"**{ca.name}** · **{ca.current_hp}** / {ca.max_hp} HP · bench ×{len(combat.challenger_lineup) - 1}",
        inline=True,
    )
    e.add_field(
        name="Opponent (active)",
        value=f"**{oa.name}** · **{oa.current_hp}** / {oa.max_hp} HP · bench ×{len(combat.opponent_lineup) - 1}",
        inline=True,
    )
    if combat.bet > 0:
        e.set_footer(text=f"Pot: {format_pokedollars(combat.bet * 2)}")
    if ca.image_small:
        e.set_thumbnail(url=ca.image_small)
    if oa.image_large:
        e.set_image(url=oa.image_large)
    return e


def _victory_embed(combat: DuelRuntime, winner_id: int) -> discord.Embed:
    pot = ""
    if combat.bet > 0:
        pot = f"\n**Winnings:** {format_pokedollars(combat.bet * 2)} (both stakes)."
    return discord.Embed(
        title="Duel over",
        description=(
            f"<@{winner_id}> **wins**!{pot}\n\n"
            + _truncate("\n\n".join(combat.log_lines[-8:]), 3000)
        ),
    )


class DuelInviteView(discord.ui.View):
    def __init__(self, cog: "DuelCog", lobby_id: str) -> None:
        super().__init__(timeout=600.0)
        self.cog = cog
        self.lobby_id = lobby_id

    def _lobby(self) -> DuelLobby | None:
        return self.cog._lobbies.get(self.lobby_id)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, row=0)
    async def accept(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        lobby = self._lobby()
        if lobby is None:
            await interaction.response.send_message("This challenge expired.", ephemeral=True)
            return
        if interaction.user.id != lobby.opponent_id:
            await interaction.response.send_message("Only the challenged player can accept.", ephemeral=True)
            return
        lobby.challenger_ready = False
        lobby.opponent_ready = False
        opp = interaction.guild.get_member(lobby.opponent_id) if interaction.guild else None
        mention = opp.mention if opp else f"<@{lobby.opponent_id}>"
        emb = _ready_embed(lobby)
        emb.description = (emb.description or "") + f"\n\n_Opponent {mention} accepted._"
        await interaction.response.edit_message(embed=emb, view=DuelReadyView(self.cog, self.lobby_id))

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, row=0)
    async def decline(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        lobby = self._lobby()
        if lobby is None:
            await interaction.response.send_message("This challenge expired.", ephemeral=True)
            return
        if interaction.user.id != lobby.opponent_id:
            await interaction.response.send_message("Only the challenged player can decline.", ephemeral=True)
            return
        self.cog._lobbies.pop(self.lobby_id, None)
        await interaction.response.edit_message(
            content="Challenge **declined**.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Cancel challenge", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_challenger(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        lobby = self._lobby()
        if lobby is None:
            await interaction.response.send_message("This challenge expired.", ephemeral=True)
            return
        if interaction.user.id != lobby.challenger_id:
            await interaction.response.send_message("Only the challenger can cancel.", ephemeral=True)
            return
        self.cog._lobbies.pop(self.lobby_id, None)
        await interaction.response.edit_message(
            content="Challenge **cancelled**.",
            embed=None,
            view=None,
        )

    async def on_timeout(self) -> None:
        self.cog._lobbies.pop(self.lobby_id, None)
        for c in self.children:
            c.disabled = True
        msg = getattr(self, "message", None)
        if msg:
            try:
                await msg.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


class DuelReadyView(discord.ui.View):
    def __init__(self, cog: "DuelCog", lobby_id: str) -> None:
        super().__init__(timeout=600.0)
        self.cog = cog
        self.lobby_id = lobby_id

    def _lobby(self) -> DuelLobby | None:
        return self.cog._lobbies.get(self.lobby_id)

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.primary, row=0)
    async def ready_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        lobby = self._lobby()
        if lobby is None:
            await interaction.response.send_message("This lobby expired.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid == lobby.challenger_id:
            lobby.challenger_ready = True
        elif uid == lobby.opponent_id:
            lobby.opponent_ready = True
        else:
            await interaction.response.send_message("You’re not in this duel.", ephemeral=True)
            return

        if lobby.challenger_ready and lobby.opponent_ready:
            await interaction.response.defer()
            await self.cog._start_combat_from_lobby(interaction, lobby)
            return

        await interaction.response.edit_message(embed=_ready_embed(lobby), view=self)

    @discord.ui.button(label="Back out", style=discord.ButtonStyle.danger, row=0)
    async def back_out(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        lobby = self._lobby()
        if lobby is None:
            await interaction.response.send_message("This lobby expired.", ephemeral=True)
            return
        if interaction.user.id not in (lobby.challenger_id, lobby.opponent_id):
            await interaction.response.send_message("You’re not in this duel.", ephemeral=True)
            return
        self.cog._lobbies.pop(self.lobby_id, None)
        await interaction.response.edit_message(
            content="Duel **aborted** before start.",
            embed=None,
            view=None,
        )

    async def on_timeout(self) -> None:
        self.cog._lobbies.pop(self.lobby_id, None)
        for c in self.children:
            c.disabled = True
        msg = getattr(self, "message", None)
        if msg:
            try:
                await msg.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


class CombatTurnView(discord.ui.View):
    def __init__(self, cog: "DuelCog", combat_id: str) -> None:
        super().__init__(timeout=3600.0)
        self.cog = cog
        self.combat_id = combat_id
        self._add_attack_select()

    def _combat(self) -> DuelRuntime | None:
        return self.cog._combats.get(self.combat_id)

    def _add_attack_select(self) -> None:
        combat = self._combat()
        if combat is None:
            return
        uid = combat.current_turn_user_id()
        side = combat.turn
        fighter = combat.challenger_lineup[0] if side == "challenger" else combat.opponent_lineup[0]
        options: list[discord.SelectOption] = []
        for i, mv in enumerate(fighter.attacks[:25]):
            dmg = int(mv["damage_int"])
            nm = str(mv["name"])
            desc = f"{dmg} damage"
            tx = mv.get("text")
            if isinstance(tx, str) and tx.strip():
                desc = _truncate(f"{dmg} dmg · {tx.strip()}", 100)
            options.append(
                discord.SelectOption(
                    label=_truncate(nm, 100),
                    value=str(i),
                    description=_truncate(desc, 100),
                )
            )
        if not options:
            return

        select = discord.ui.Select(
            custom_id=f"atk:{self.combat_id[:16]}",
            placeholder=f"Choose a move (<@{uid}>’s turn)",
            options=options,
            row=0,
        )

        async def select_cb(inter: discord.Interaction) -> None:
            if inter.user.id != uid:
                await inter.response.send_message("It’s not your turn.", ephemeral=True)
                return
            raw = inter.data.get("values", [""])[0] if inter.data else ""
            try:
                idx = int(raw)
            except ValueError:
                await inter.response.send_message("Invalid choice.", ephemeral=True)
                return
            await self.cog._resolve_attack(inter, self.combat_id, idx)

        select.callback = select_cb
        self.add_item(select)


class DuelCog(commands.Cog):
    """Betting duels with saved combat decks."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wallet = WalletService()
        self._lobbies: dict[str, DuelLobby] = {}
        self._combats: dict[str, DuelRuntime] = {}
        self._rng = random.Random()

    async def _revert_ready_ui(self, interaction: discord.Interaction, lobby: DuelLobby) -> None:
        lobby.challenger_ready = lobby.opponent_ready = False
        ch = interaction.channel
        if ch is None or lobby.message_id is None:
            return
        try:
            msg = await ch.fetch_message(lobby.message_id)
            await msg.edit(embed=_ready_embed(lobby), view=DuelReadyView(self, lobby.lobby_id))
        except (discord.HTTPException, discord.NotFound):
            pass

    def _user_busy(self, user_id: int) -> bool:
        for l in self._lobbies.values():
            if user_id in (l.challenger_id, l.opponent_id):
                return True
        for c in self._combats.values():
            if user_id in (c.challenger_id, c.opponent_id):
                return True
        return False

    async def _start_combat_from_lobby(self, interaction: discord.Interaction, lobby: DuelLobby) -> None:
        assert lobby.message_id is not None
        channel = interaction.channel
        if channel is None:
            return
        lid = lobby.lobby_id
        try:
            async with self.bot.async_session_factory() as session:
                ch_ids = await get_saved_instance_ids(session, lobby.challenger_id)
                op_ids = await get_saved_instance_ids(session, lobby.opponent_id)
                if not ch_ids:
                    await self._revert_ready_ui(interaction, lobby)
                    await interaction.followup.send(
                        f"<@{lobby.challenger_id}> needs a combat deck — use **`/deck set`**.",
                        ephemeral=False,
                    )
                    return
                if not op_ids:
                    await self._revert_ready_ui(interaction, lobby)
                    await interaction.followup.send(
                        f"<@{lobby.opponent_id}> needs a combat deck — use **`/deck set`**.",
                        ephemeral=False,
                    )
                    return
                ch_pairs = await load_fighters_ordered(session, lobby.challenger_id, ch_ids)
                if isinstance(ch_pairs, str):
                    await self._revert_ready_ui(interaction, lobby)
                    await interaction.followup.send(ch_pairs, ephemeral=False)
                    return
                op_pairs = await load_fighters_ordered(session, lobby.opponent_id, op_ids)
                if isinstance(op_pairs, str):
                    await self._revert_ready_ui(interaction, lobby)
                    await interaction.followup.send(op_pairs, ephemeral=False)
                    return
                ch_line = [Fighter.from_instance(i, c) for i, c in ch_pairs]
                op_line = [Fighter.from_instance(i, c) for i, c in op_pairs]
                if lobby.bet > 0:
                    try:
                        await self._wallet.try_debit(session, lobby.challenger_id, lobby.bet)
                        await self._wallet.try_debit(session, lobby.opponent_id, lobby.bet)
                    except InsufficientPokedollarsError:
                        await session.rollback()
                        await self._revert_ready_ui(interaction, lobby)
                        await interaction.followup.send(
                            "One or both players can’t cover the stake — duel not started.",
                            ephemeral=False,
                        )
                        return
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("duel start DB")
            await self._revert_ready_ui(interaction, lobby)
            await interaction.followup.send("Database error — try again.", ephemeral=False)
            return

        combat = DuelRuntime.from_lineups(
            lobby_id=lid,
            challenger_id=lobby.challenger_id,
            opponent_id=lobby.opponent_id,
            bet=lobby.bet,
            challenger_lineup=ch_line,
            opponent_lineup=op_line,
            rng=self._rng,
        )
        self._combats[lid] = combat
        self._lobbies.pop(lid, None)
        emb = _combat_embed(combat)
        view = CombatTurnView(self, lid)
        msg = await channel.fetch_message(lobby.message_id)
        await msg.edit(embed=emb, view=view)

    async def _resolve_attack(
        self,
        interaction: discord.Interaction,
        combat_id: str,
        attack_index: int,
    ) -> None:
        combat = self._combats.get(combat_id)
        if combat is None:
            await interaction.response.send_message("This duel is over or missing.", ephemeral=True)
            return
        if interaction.user.id != combat.current_turn_user_id():
            await interaction.response.send_message("Not your turn.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            winner = combat.apply_attack(attack_index)
        except ValueError:
            await interaction.followup.send("Invalid attack.", ephemeral=True)
            return

        channel = interaction.channel
        msg = interaction.message
        if channel is None or msg is None:
            return

        if winner is not None:
            try:
                async with self.bot.async_session_factory() as session:
                    if combat.bet > 0:
                        await self._wallet.try_credit(session, winner, combat.bet * 2)
                    await session.commit()
            except SQLAlchemyError:
                _LOG.exception("duel payout")
                await interaction.followup.send(
                    "Duel finished but payout failed — contact an admin.",
                    ephemeral=False,
                )
            self._combats.pop(combat_id, None)
            ve = _victory_embed(combat, winner)
            await msg.edit(embed=ve, view=None)
            return

        emb = _combat_embed(combat)
        view = CombatTurnView(self, combat_id)
        if emb is None:
            await msg.edit(content="Duel state error.", embed=None, view=None)
            self._combats.pop(combat_id, None)
            return
        await msg.edit(embed=emb, view=view)

    @commands.hybrid_group(name="duel", description="Pokémon TCG duels", invoke_without_command=True)
    async def duel_group(self, ctx: commands.Context) -> None:
        await ctx.send(
            "Use **`/duel challenge`** (slash) or **`c duel challenge @user <bet>`**.\n"
            "Save **1–6** Pokémon with **`/deck set`** first.",
            ephemeral=_hybrid_ephemeral(ctx),
        )

    @duel_group.command(name="challenge", aliases=["chal"])
    @app_commands.describe(
        opponent="Player to duel",
        bet="Each player stakes this much (0 = friendly)",
    )
    async def duel_challenge(
        self,
        ctx: commands.Context,
        opponent: discord.Member,
        bet: app_commands.Range[int, 0, 500_000] = 0,
    ) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        if opponent.id == ctx.author.id:
            await ctx.send("You can’t duel yourself.", ephemeral=_hybrid_ephemeral(ctx))
            return
        if opponent.bot:
            await ctx.send("Pick a human opponent.", ephemeral=_hybrid_ephemeral(ctx))
            return
        uid = ctx.author.id
        if self._user_busy(uid) or self._user_busy(opponent.id):
            await ctx.send("You or your opponent is already in a duel or challenge.", ephemeral=_hybrid_ephemeral(ctx))
            return

        lid = secrets.token_hex(4)
        lobby = DuelLobby(
            lobby_id=lid,
            challenger_id=uid,
            opponent_id=opponent.id,
            bet=int(bet),
            channel_id=ctx.channel.id if ctx.channel else 0,
        )
        emb = _invite_embed(lobby, opponent)
        view = DuelInviteView(self, lid)
        m = await ctx.send(embed=emb, view=view)
        lobby.message_id = m.id
        self._lobbies[lid] = lobby

    @commands.hybrid_group(name="deck", description="Your duel combat deck", invoke_without_command=True)
    async def deck_group(self, ctx: commands.Context) -> None:
        await ctx.send(
            f"**`/deck set`** — paste **{MIN_DECK}–{MAX_DECK}** Card IDs you own (space-separated). "
            f"Order = lead Pokémon first.\n**`/deck view`** — saved bench.",
            ephemeral=_hybrid_ephemeral(ctx),
        )

    @deck_group.command(name="set", aliases=["s"])
    @app_commands.describe(card_ids=f"Space-separated Card IDs ({MIN_DECK}–{MAX_DECK})")
    async def deck_set(self, ctx: commands.Context, *, card_ids: str) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        parts = [p for p in card_ids.replace(",", " ").split() if p.strip()]
        uid = ctx.author.id
        try:
            async with self.bot.async_session_factory() as session:
                err = await set_deck_from_public_ids(session, uid, parts)
                if err:
                    await ctx.send(err, ephemeral=ephe)
                    return
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("deck set %s", uid)
            await ctx.send("Could not save deck.", ephemeral=ephe)
            return
        await ctx.send(
            f"Combat deck saved (**{len(parts)}** Pokémon). Order: lead → bench.",
            ephemeral=ephe,
        )

    @deck_group.command(name="view", aliases=["v"])
    async def deck_view(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        try:
            async with self.bot.async_session_factory() as session:
                ids = await get_saved_instance_ids(session, uid)
                if not ids:
                    await ctx.send("No deck saved yet — use **`/deck set`**.", ephemeral=ephe)
                    return
                lines: list[str] = []
                for n, iid in enumerate(ids, start=1):
                    inst = await session.get(UserCardInstance, iid)
                    if inst is None:
                        lines.append(f"`{n}.` _(missing instance)_")
                        continue
                    card = await session.get(Card, inst.card_id)
                    if card is None:
                        lines.append(f"`{n}.` _(missing card)_")
                        continue
                    nm = _truncate(card.name, 22)
                    pid = compact_public_id_for_line(inst.public_id)
                    lines.append(f"`{n}.` **{nm}** `{pid}`")
        except SQLAlchemyError:
            _LOG.exception("deck view %s", uid)
            await ctx.send("Could not load deck.", ephemeral=ephe)
            return
        await ctx.send(
            embed=discord.Embed(title="Your combat deck", description="\n".join(lines)),
            ephemeral=ephe,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DuelCog(bot))
