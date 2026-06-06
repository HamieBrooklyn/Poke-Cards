"""Pokémon card duels: bet, combat deck, turn-based combat from catalog stats."""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
from dataclasses import dataclass
import discord
from discord import app_commands
from discord.ext import commands
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.services.missions import MissionService
from poke_pon_bot.services.mission_notifications import schedule_mission_completion_dms
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.instance_public_id import compact_public_id_for_line
from poke_pon_bot.services.combat_deck import (
    MAX_DECK,
    MIN_DECK,
    deck_slots_embed_body,
    get_saved_instance_ids,
    load_deck_slots_padded,
    load_fighters_ordered,
    persist_deck_slots,
    resolve_owned_instance_by_public_id,
)
from poke_pon_bot.services.duel_engine import (
    DuelRuntime,
    Fighter,
    best_attack_index,
    normalized_attacks,
    parse_hp,
)
from poke_pon_bot.services.type_effectiveness import (
    combined_type_factor,
    defense_types_from_card_types,
    move_attacking_chart_type,
    scaled_damage,
)
from poke_pon_bot.services.pack_collage import (
    render_single_card_png_from_url,
    render_url_vs_collage_png,
)
from poke_pon_bot.services.crystals import CrystalsService, format_crystals
from poke_pon_bot.services.wallet import (
    InsufficientPokedollarsError,
    WalletService,
    format_pokedollars,
)

_LOG = logging.getLogger(__name__)

DECK_EDITOR_WEB_URL = "https://hamiebrooklyn.github.io/deck.html"


def _deck_web_footer() -> str:
    return (
        "\n\n🌐 **Easier deck editing on the web →**\n"
        f"<{DECK_EDITOR_WEB_URL}>"
    )


_PVE_LOSS_MIN = 20
_PVE_LOSS_MAX = 70

_PVE_CRYSTAL_RARITY_MIN = 7   # Illustration Rare or higher
_PVE_CRYSTAL_HP_MIN = 220
_PVE_CRYSTAL_DMG_MIN = 170
_PVE_CRYSTAL_REWARD = 1


def _wild_max_attack_damage(card: Card) -> int:
    attacks = normalized_attacks(card)
    return max((int(a.get("damage_int") or 0) for a in attacks), default=0)


async def _render_vs_collage_file(
    *,
    left_name: str,
    left_url: str | None,
    right_name: str,
    right_url: str | None,
) -> discord.File | None:
    if not left_url or not right_url:
        return None
    png = await render_url_vs_collage_png([left_url, right_url], labels=[left_name, right_name], gap_label="VS")
    if png is None:
        return None
    return discord.File(png, filename="vs.png")


async def _winner_card_file(*, combat: DuelRuntime, winner_id: int) -> discord.File | None:
    if winner_id == combat.challenger_id:
        fighter = combat.challenger_lineup[0] if combat.challenger_lineup else None
    elif winner_id == combat.opponent_id:
        fighter = combat.opponent_lineup[0] if combat.opponent_lineup else None
    else:
        fighter = None
    url = None
    label = ""
    if fighter is not None:
        label = fighter.name
        url = fighter.image_large or fighter.image_small
    png = await render_single_card_png_from_url(url or "", label=label)
    if png is None:
        return None
    return discord.File(png, filename="winner.png")


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
            f"Set your bench with **`/deck edit`** (or **`c deck edit`** …), then press **Ready**.\n\n"
            f"{cr} Challenger ready\n"
            f"{or_} Opponent ready"
        ),
    )


def _attack_button_label(move: dict, *, defender: Fighter | None) -> str:
    """Show expected damage after type modifiers (vs current defender)."""
    base = int(move["damage_int"])
    nm = str(move["name"])
    if defender is None:
        return _truncate(f"{nm} ({base})", 80)
    cost = move.get("cost")
    if not isinstance(cost, list):
        cost = None
    atk_t = move_attacking_chart_type(cost)
    fac = combined_type_factor(atk_t, defender.types)
    eff = scaled_damage(base, fac)
    if abs(float(fac) - 1.0) < 1e-9:
        return _truncate(f"{nm} ({eff})", 80)
    fs = str(int(fac)) if float(fac) == int(fac) else f"{float(fac):g}"
    return _truncate(f"{nm} ({eff} ×{fs})", 80)


_HP_BAR_LEN = 10


def _hp_bar(current: int, maximum: int) -> str:
    """Visual HP bar: ``[████████░░] 120/200``."""
    ratio = max(0.0, min(1.0, current / maximum)) if maximum > 0 else 0.0
    filled = round(ratio * _HP_BAR_LEN)
    empty = _HP_BAR_LEN - filled
    if ratio > 0.5:
        bar = "🟩" * filled + "⬛" * empty
    elif ratio > 0.2:
        bar = "🟨" * filled + "⬛" * empty
    else:
        bar = "🟥" * filled + "⬛" * empty
    return f"{bar} **{max(0, current)}**/{maximum}"


def _type_eff_indicator(factor: float) -> str:
    """Emoji + label for type effectiveness."""
    if factor >= 2.0:
        return "🔥 **Super effective!**"
    if factor > 1.0:
        return "💥 **Super effective!**"
    if factor < 0.5:
        return "🛡️ **Barely effective…**"
    if factor < 1.0:
        return "🛡️ **Not very effective…**"
    return ""


def _last_move_line(combat: DuelRuntime) -> str:
    """Compact single-line summary of the most recent attack (from metadata)."""
    meta = getattr(combat, "_last_move_meta", None)
    if meta is None:
        return ""
    eff = _type_eff_indicator(meta["factor"])
    eff_part = f"\n{eff}" if eff else ""
    return (
        f"**{meta['attacker']}** used **{meta['move']}** → "
        f"**{meta['damage']}** dmg to **{meta['defender']}**{eff_part}"
    )


def _combat_embeds(combat: DuelRuntime) -> list[discord.Embed] | None:
    if not combat.challenger_lineup or not combat.opponent_lineup:
        return None
    ca = combat.challenger_lineup[0]
    oa = combat.opponent_lineup[0]
    turn_uid = combat.current_turn_user_id()
    last_move = _last_move_line(combat)
    desc = f"**Turn:** <@{turn_uid}>"
    if last_move:
        desc += f"\n\n{last_move}"
    main = discord.Embed(title="Duel — battle", description=_truncate(desc, 4096))
    main.add_field(
        name=f"⚔️ {ca.name}",
        value=f"{_hp_bar(ca.current_hp, ca.max_hp)}\nbench ×{len(combat.challenger_lineup) - 1}",
        inline=True,
    )
    main.add_field(
        name=f"🎯 {oa.name}",
        value=f"{_hp_bar(oa.current_hp, oa.max_hp)}\nbench ×{len(combat.opponent_lineup) - 1}",
        inline=True,
    )
    if combat.bet > 0:
        main.set_footer(text=f"Pot: {format_pokedollars(combat.bet * 2)}")
    return [main]


def _victory_embed(combat: DuelRuntime, winner_id: int) -> discord.Embed:
    pot = ""
    if combat.bet > 0:
        pot = f"\n**Winnings:** {format_pokedollars(combat.bet * 2)} (both stakes)."
    last_move = _last_move_line(combat)
    desc = f"<@{winner_id}> **wins**!{pot}"
    if last_move:
        desc += f"\n\n{last_move}"
    return discord.Embed(title="Duel over", description=_truncate(desc, 4096))


def _difficulty_reward_range(*, rarity_class_id: int, hp: int) -> tuple[int, int]:
    r = max(1, int(rarity_class_id))
    h = max(1, int(hp))
    tier = min(12, r)
    hp_band = min(8, h // 40)
    base = 20 + tier * 10 + hp_band * 6
    lo = max(10, base - 10)
    hi = base + 20
    return int(lo), int(hi)


def _fighter_from_catalog_card(card: Card) -> Fighter:
    hp = max(1, parse_hp(card.hp))
    sm = card.image_small_url or card.image_large_url
    lg = card.image_large_url or card.image_small_url
    return Fighter(
        instance_id=0,
        public_id="WILD",
        name=card.name,
        image_small=sm,
        image_large=lg,
        max_hp=hp,
        current_hp=hp,
        types=defense_types_from_card_types(card.tcg_types, supertype=card.supertype),
        attacks=normalized_attacks(card),
    )


def _pve_embeds(combat: DuelRuntime, *, wild_name: str, wild_image: str | None, user_id: int) -> list[discord.Embed] | None:
    if not combat.challenger_lineup or not combat.opponent_lineup:
        return None
    player = combat.challenger_lineup[0]
    wild = combat.opponent_lineup[0]
    turn_label = f"<@{user_id}>" if combat.turn == "challenger" else f"**{wild_name}**"
    last_move = _last_move_line(combat)
    desc = f"**Turn:** {turn_label}"
    if last_move:
        desc += f"\n\n{last_move}"
    main = discord.Embed(
        title="Poke-duel — battle",
        description=_truncate(desc, 4096),
    )
    main.add_field(
        name=f"⚔️ {player.name}",
        value=_hp_bar(player.current_hp, player.max_hp),
        inline=True,
    )
    main.add_field(
        name=f"🎯 {wild_name}",
        value=_hp_bar(wild.current_hp, wild.max_hp),
        inline=True,
    )
    return [main]


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
        self._add_attack_buttons()

    def _combat(self) -> DuelRuntime | None:
        return self.cog._combats.get(self.combat_id)

    def _add_attack_buttons(self) -> None:
        combat = self._combat()
        if combat is None:
            return
        uid = combat.current_turn_user_id()
        side = combat.turn
        fighter = combat.challenger_lineup[0] if side == "challenger" else combat.opponent_lineup[0]
        defender = combat.opponent_lineup[0] if side == "challenger" else combat.challenger_lineup[0]
        attacks = fighter.attacks[:25]
        if not attacks:
            return

        for i, mv in enumerate(attacks):
            label = _attack_button_label(mv, defender=defender)
            row = i // 5
            if row >= 5:
                break

            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                row=row,
                custom_id=f"atkbtn:{self.combat_id[:8]}:{i}",
            )

            async def btn_cb(inter: discord.Interaction, idx: int = i) -> None:
                if inter.user.id != uid:
                    await inter.response.send_message("It’s not your turn.", ephemeral=True)
                    return
                await self.cog._resolve_attack(inter, self.combat_id, idx)

            button.callback = btn_cb
            self.add_item(button)


class PveTurnView(discord.ui.View):
    def __init__(self, cog: "DuelCog", combat_id: str, *, user_id: int) -> None:
        super().__init__(timeout=1800.0)
        self.cog = cog
        self.combat_id = combat_id
        self.user_id = user_id
        self._add_attack_buttons()

    def _combat(self) -> DuelRuntime | None:
        return self.cog._combats.get(self.combat_id)

    def _add_attack_buttons(self) -> None:
        combat = self._combat()
        if combat is None or not combat.challenger_lineup:
            return
        fighter = combat.challenger_lineup[0]
        defender = combat.opponent_lineup[0] if combat.opponent_lineup else None
        attacks = fighter.attacks[:25]
        if not attacks:
            return

        for i, mv in enumerate(attacks):
            label = _attack_button_label(mv, defender=defender)
            row = i // 5
            if row >= 5:
                break

            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                row=row,
                custom_id=f"pveatk:{self.combat_id[:8]}:{i}",
            )

            async def btn_cb(inter: discord.Interaction, idx: int = i) -> None:
                if inter.user.id != self.user_id:
                    await inter.response.send_message("This isn’t your battle.", ephemeral=True)
                    return
                await self.cog._resolve_pve_attack(inter, self.combat_id, idx)

            button.callback = btn_cb
            self.add_item(button)


class NextFightView(discord.ui.View):
    """Post-fight view with a 'Next Fight' button to instantly re-queue /pd."""

    def __init__(self, cog: "DuelCog", user_id: int) -> None:
        super().__init__(timeout=120.0)
        self.cog = cog
        self.user_id = user_id

    @discord.ui.button(label="Next Fight", style=discord.ButtonStyle.success, emoji="⚔️")
    async def next_fight(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your fight.", ephemeral=True)
            return
        self.stop()
        button.disabled = True
        await interaction.response.defer()
        await self.cog._start_pve_from_interaction(interaction, edit_message=interaction.message)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


class DeckEditView(discord.ui.View):
    """Bench editor: pick slot from dropdown, reply with Card ID, or clear the slot."""

    def __init__(self, cog: "DuelCog", owner_id: int, slots: list[int | None]) -> None:
        super().__init__(timeout=600.0)
        self.cog = cog
        self.owner_id = owner_id
        self.slots = slots
        self.selected_idx = 0
        self.panel_message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn’t your deck editor.", ephemeral=True)
            return False
        return True

    async def build_embed(self, session: AsyncSession) -> discord.Embed:
        body = await deck_slots_embed_body(session, self.owner_id, self.slots)
        sel = self.selected_idx + 1
        lead = " *(lead)*" if self.selected_idx == 0 else ""
        desc = (
            f"{body}\n\n"
            f"**Selected:** slot **{sel}**{lead}\n"
            "Reply in this channel with a **Card ID** to assign it to that slot.\n"
            "**Clear slot** removes the selected seat. **Done** closes the editor.\n"
            "_Requires the bot to see your message (Message Content Intent in servers)._"
            + _deck_web_footer()
        )
        return discord.Embed(title="Edit combat deck", description=_truncate(desc, 4096))

    async def rebuild_items(self, session: AsyncSession) -> None:
        self.clear_items()
        options: list[discord.SelectOption] = []
        for i in range(MAX_DECK):
            sid = self.slots[i]
            lead = "★ " if i == 0 else ""
            if sid is None:
                label = _truncate(f"{lead}Slot {i + 1} — Empty", 100)
                desc = "Lead Pokémon" if i == 0 else "Bench seat"
            else:
                inst = await session.get(UserCardInstance, sid)
                nm = "?"
                pid_line = ""
                if inst is not None:
                    card = await session.get(Card, inst.card_id)
                    if card is not None:
                        nm = _truncate(card.name, 42)
                    pid_line = compact_public_id_for_line(inst.public_id)
                label = _truncate(f"{lead}Slot {i + 1} — {nm}", 100)
                desc = _truncate(pid_line or "Owned copy", 100)
            options.append(discord.SelectOption(label=label, value=str(i), description=desc))
        slot_select = discord.ui.Select(
            custom_id="deck_editor_slot",
            placeholder="Bench slot…",
            options=options,
            row=0,
        )

        async def slot_cb(interaction: discord.Interaction) -> None:
            self.selected_idx = int(interaction.data["values"][0])
            async with self.cog.bot.async_session_factory() as sess:
                embed = await self.build_embed(sess)
                await self.rebuild_items(sess)
            await interaction.response.edit_message(embed=embed, view=self)

        slot_select.callback = slot_cb
        self.add_item(slot_select)

        empty_selected = self.slots[self.selected_idx] is None
        clear_btn = discord.ui.Button(
            label="Clear slot",
            style=discord.ButtonStyle.danger,
            row=1,
            disabled=empty_selected,
        )

        async def clear_cb(interaction: discord.Interaction) -> None:
            prev = self.slots[self.selected_idx]
            self.slots[self.selected_idx] = None
            async with self.cog.bot.async_session_factory() as sess:
                err = await persist_deck_slots(sess, self.owner_id, self.slots)
                if err:
                    self.slots[self.selected_idx] = prev
                    await sess.rollback()
                    await interaction.response.send_message(err, ephemeral=True)
                    return
                await sess.commit()
                embed = await self.build_embed(sess)
                await self.rebuild_items(sess)
            await interaction.response.edit_message(embed=embed, view=self)

        clear_btn.callback = clear_cb
        self.add_item(clear_btn)

        done_btn = discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, row=1)

        async def done_cb(interaction: discord.Interaction) -> None:
            self.cog._stop_deck_edit_listener(self.owner_id)
            compact_n = sum(1 for x in self.slots if x is not None)
            foot = (
                f"Saved bench: **{compact_n}** Pokémon."
                if compact_n
                else "**No deck saved.** Use **`/deck edit`** to build one."
            )
            emb = discord.Embed(title="Deck editor closed", description=foot)
            await interaction.response.edit_message(embed=emb, view=None)
            self.stop()

        done_btn.callback = done_cb
        self.add_item(done_btn)

    async def handle_card_message(self, msg: discord.Message) -> None:
        raw = msg.content.strip()
        if not raw:
            return
        prev_slot = self.slots[self.selected_idx]
        try:
            async with self.cog.bot.async_session_factory() as session:
                iid, err = await resolve_owned_instance_by_public_id(session, self.owner_id, raw)
                if err:
                    await msg.reply(err, delete_after=25)
                    return
                for j, ex in enumerate(self.slots):
                    if j != self.selected_idx and ex == iid:
                        await msg.reply(f"You already have that copy in **slot {j + 1}**.", delete_after=25)
                        return
                self.slots[self.selected_idx] = iid
                err2 = await persist_deck_slots(session, self.owner_id, self.slots)
                if err2:
                    self.slots[self.selected_idx] = prev_slot
                    await session.rollback()
                    await msg.reply(err2, delete_after=30)
                    return
                await session.commit()
            try:
                await msg.add_reaction("✅")
            except discord.HTTPException:
                pass
            async with self.cog.bot.async_session_factory() as session:
                embed = await self.build_embed(session)
                await self.rebuild_items(session)
            if self.panel_message:
                try:
                    await self.panel_message.edit(embed=embed, view=self)
                except discord.HTTPException:
                    pass
        except SQLAlchemyError:
            _LOG.exception("deck edit assign for user %s", self.owner_id)
            await msg.reply("Could not save that card. Try again.", delete_after=25)

    async def on_timeout(self) -> None:
        self.cog._stop_deck_edit_listener(self.owner_id)
        if self.panel_message:
            try:
                emb = discord.Embed(
                    title="Deck editor — timed out",
                    description="Run **`/deck edit`** again to continue.",
                )
                await self.panel_message.edit(embed=emb, view=None)
            except discord.HTTPException:
                pass


class DuelCog(commands.Cog):
    """Betting duels with saved combat decks."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wallet = WalletService()
        self._lobbies: dict[str, DuelLobby] = {}
        self._combats: dict[str, DuelRuntime] = {}
        self._pve_meta: dict[str, dict[str, object]] = {}
        self._rng = random.Random()
        self._deck_edit_tasks: dict[int, asyncio.Task] = {}

    def _stop_deck_edit_listener(self, uid: int) -> None:
        task = self._deck_edit_tasks.pop(uid, None)
        if task is not None and not task.done():
            task.cancel()

    async def _deck_edit_listener(self, view: DeckEditView) -> None:
        uid = view.owner_id
        panel = view.panel_message
        if panel is None:
            return
        channel = panel.channel
        try:
            while not view.is_finished():

                def check(m: discord.Message) -> bool:
                    return m.channel.id == channel.id and m.author.id == uid and not m.author.bot

                try:
                    incoming = await self.bot.wait_for("message", timeout=150.0, check=check)
                except asyncio.TimeoutError:
                    continue
                if view.is_finished():
                    break
                await view.handle_card_message(incoming)
        except asyncio.CancelledError:
            pass
        finally:
            self._deck_edit_tasks.pop(uid, None)

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
                        f"<@{lobby.challenger_id}> needs a combat deck — use **`/deck edit`**.",
                        ephemeral=False,
                    )
                    return
                if not op_ids:
                    await self._revert_ready_ui(interaction, lobby)
                    await interaction.followup.send(
                        f"<@{lobby.opponent_id}> needs a combat deck — use **`/deck edit`**.",
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
        file: discord.File | None = None
        embeds = _combat_embeds(combat)
        if embeds:
            ca = combat.challenger_lineup[0]
            oa = combat.opponent_lineup[0]
            file = await _render_vs_collage_file(
                left_name=ca.name,
                left_url=ca.image_large or ca.image_small,
                right_name=oa.name,
                right_url=oa.image_large or oa.image_small,
            )
            if file is not None:
                embeds[0].set_image(url="attachment://vs.png")
        view = CombatTurnView(self, lid)
        msg = await channel.fetch_message(lobby.message_id)
        if file is not None:
            await msg.edit(embeds=embeds, view=view, attachments=[file])
        else:
            await msg.edit(embeds=embeds, view=view)

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
            duel_win_notices: list = []
            try:
                async with self.bot.async_session_factory() as session:
                    if combat.bet > 0:
                        await self._wallet.try_credit(session, winner, combat.bet * 2)
                    try:
                        duel_win_notices = await MissionService().record_duel_win(
                            session, winner
                        )
                    except Exception:
                        _LOG.exception("mission record_duel_win failed user=%s", winner)
                    await session.commit()
            except SQLAlchemyError:
                _LOG.exception("duel payout")
                await interaction.followup.send(
                    "Duel finished but payout failed — contact an admin.",
                    ephemeral=False,
                )
            else:
                schedule_mission_completion_dms(
                    self.bot, user_id=winner, notices=duel_win_notices
                )
            ve = _victory_embed(combat, winner)
            win_file = await _winner_card_file(combat=combat, winner_id=winner)
            if win_file is not None:
                ve.set_image(url="attachment://winner.png")
                await msg.edit(embed=ve, view=None, attachments=[win_file])
            else:
                await msg.edit(embed=ve, view=None)
            self._combats.pop(combat_id, None)
            return

        embeds = _combat_embeds(combat)
        view = CombatTurnView(self, combat_id)
        if embeds is None:
            await msg.edit(content="Duel state error.", embed=None, view=None)
            self._combats.pop(combat_id, None)
            return
        file: discord.File | None = None
        if embeds:
            ca = combat.challenger_lineup[0]
            oa = combat.opponent_lineup[0]
            file = await _render_vs_collage_file(
                left_name=ca.name,
                left_url=ca.image_large or ca.image_small,
                right_name=oa.name,
                right_url=oa.image_large or oa.image_small,
            )
            if file is not None:
                embeds[0].set_image(url="attachment://vs.png")
        if file is not None:
            await msg.edit(embeds=embeds, view=view, attachments=[file])
        else:
            await msg.edit(embeds=embeds, view=view)

    async def _pve_bot_step(self, combat_id: str) -> int | None:
        """Advance the wild bot until it's the player's turn or duel ends. Returns winner user id (or 0 for wild)."""
        combat = self._combats.get(combat_id)
        if combat is None:
            return None
        winner: int | None = None
        guard = 0
        while winner is None and combat.turn == "opponent" and guard < 10:
            atk = combat.opponent_lineup[0]
            if not atk.attacks:
                combat.turn = "challenger"
                combat.log_lines.append("_Wild has no usable attacks — your turn._")
                return None
            # Bot chooses strongest move (simple, deterministic difficulty).
            def_f = combat.challenger_lineup[0] if combat.challenger_lineup else None
            idx = (
                best_attack_index(atk, def_f)
                if def_f is not None
                else max(range(len(atk.attacks)), key=lambda k: int(atk.attacks[k]["damage_int"]))
            )
            winner = combat.apply_attack(idx)
            guard += 1
        return winner

    async def _finish_pve(
        self,
        interaction: discord.Interaction | None,
        combat_id: str,
        winner: int,
        *,
        ctx: commands.Context | None = None,
    ) -> None:
        combat_snapshot = self._combats.get(combat_id)
        meta = self._pve_meta.get(combat_id) or {}
        wild_name = str(meta.get("wild_name") or "Wild")
        wild_rarity = int(meta.get("wild_rarity_class_id") or 1)
        wild_hp = int(meta.get("wild_hp") or 1)
        wild_max_dmg = int(meta.get("wild_max_dmg") or 0)

        if interaction is not None:
            uid = interaction.user.id
        elif ctx is not None:
            uid = ctx.author.id
        else:
            _LOG.error("_finish_pve called with neither interaction nor ctx")
            return
        try:
            async with self.bot.async_session_factory() as session:
                if winner == uid:
                    lo, hi = _difficulty_reward_range(rarity_class_id=wild_rarity, hp=wild_hp)
                    reward = self._rng.randint(lo, hi)
                    new_bal = await self._wallet.try_credit(session, uid, reward)

                    crystal_bonus = 0
                    if (
                        wild_rarity >= _PVE_CRYSTAL_RARITY_MIN
                        or wild_hp >= _PVE_CRYSTAL_HP_MIN
                        or wild_max_dmg >= _PVE_CRYSTAL_DMG_MIN
                    ):
                        crystal_bonus = _PVE_CRYSTAL_REWARD
                        try:
                            await CrystalsService().try_credit(session, uid, crystal_bonus)
                        except Exception:
                            _LOG.exception("crystal bonus credit failed user=%s", uid)
                            crystal_bonus = 0

                    try:
                        duel_win_notices = await MissionService().record_duel_win(
                            session, uid
                        )
                    except Exception:
                        _LOG.exception("mission record_duel_win failed user=%s", uid)
                        duel_win_notices = []
                    await session.commit()
                    schedule_mission_completion_dms(
                        self.bot, user_id=uid, notices=duel_win_notices
                    )

                    bonus_line = ""
                    if crystal_bonus > 0:
                        bonus_line = f"\n**Bonus:** {format_crystals(crystal_bonus)} for beating a rare/strong wild!"

                    embed = discord.Embed(
                        title="Poke-duel — Victory",
                        description=(
                            f"You defeated **{wild_name}** and earned **{format_pokedollars(reward)}**!{bonus_line}\n"
                            f"**Balance:** {format_pokedollars(new_bal)}"
                        ),
                    )
                else:
                    loss = self._rng.randint(_PVE_LOSS_MIN, _PVE_LOSS_MAX)
                    bal = await self._wallet.get_balance(session, uid)
                    debit = min(bal, loss)
                    if debit > 0:
                        try:
                            new_bal = await self._wallet.try_debit(session, uid, debit)
                        except InsufficientPokedollarsError:
                            new_bal = await self._wallet.set_balance(session, uid, 0)
                    else:
                        new_bal = bal
                    await session.commit()
                    embed = discord.Embed(
                        title="Poke-duel — Defeat",
                        description=(
                            f"You lost to **{wild_name}** and dropped **{format_pokedollars(debit)}**.\n"
                            f"**Balance:** {format_pokedollars(new_bal)}"
                        ),
                    )
        except SQLAlchemyError:
            _LOG.exception("pve payout failed")
            embed = discord.Embed(
                title="Poke-duel — Finished",
                description="Battle ended, but saving the payout failed. Try again later.",
            )

        # Clean up
        self._combats.pop(combat_id, None)
        self._pve_meta.pop(combat_id, None)

        win_file: discord.File | None = None
        if winner == uid and combat_snapshot is not None:
            win_file = await _winner_card_file(combat=combat_snapshot, winner_id=winner)
        if win_file is not None:
            embed.set_image(url="attachment://winner.png")

        next_view = NextFightView(self, uid)

        # Slash `/pd` uses defer → ``interaction.message`` is usually ``None``; component turns use the battle msg.
        # Prefix **`pcpd`** / **`pd`** passes ``ctx`` so we **send** the outcome when there is no webhook to edit.
        try:
            if interaction is not None:
                if interaction.message is not None:
                    if win_file is not None:
                        await interaction.message.edit(embed=embed, view=next_view, attachments=[win_file])
                    else:
                        await interaction.message.edit(embed=embed, view=next_view)
                else:
                    if win_file is not None:
                        await interaction.edit_original_response(embed=embed, view=next_view, attachments=[win_file])
                    else:
                        await interaction.edit_original_response(embed=embed, view=next_view)
            elif ctx is not None:
                if win_file is not None:
                    await ctx.send(embed=embed, file=win_file, view=next_view, ephemeral=False)
                else:
                    await ctx.send(embed=embed, view=next_view, ephemeral=False)
        except discord.HTTPException:
            _LOG.exception("pve finish: edit failed (interaction_message=%s)", interaction and interaction.message is not None)
            try:
                if interaction is not None:
                    if win_file is not None:
                        await interaction.followup.send(embed=embed, file=win_file, view=next_view, ephemeral=False)
                    else:
                        await interaction.followup.send(embed=embed, view=next_view, ephemeral=False)
                elif ctx is not None:
                    if win_file is not None:
                        await ctx.send(embed=embed, file=win_file, view=next_view, ephemeral=False)
                    else:
                        await ctx.send(embed=embed, view=next_view, ephemeral=False)
            except discord.HTTPException:
                _LOG.exception("pve finish followup also failed")

    async def _start_pve_from_interaction(
        self,
        interaction: discord.Interaction,
        *,
        edit_message: discord.Message | None = None,
    ) -> None:
        """Start a new PvE fight triggered by the 'Next Fight' button.

        When *edit_message* is provided the new battle replaces that message
        in-place (no clutter).  Falls back to ``followup.send`` on failure.
        """
        uid = interaction.user.id
        drop = DropService(rng=self._rng)

        if self._user_busy(uid):
            await interaction.followup.send("You're already in a duel/battle.", ephemeral=True)
            return

        try:
            async with self.bot.async_session_factory() as session:
                ids = await get_saved_instance_ids(session, uid)
                if not ids:
                    await interaction.followup.send(
                        "You need a combat deck first — use **`/deck edit`**.", ephemeral=True
                    )
                    return
                pairs = await load_fighters_ordered(session, uid, ids)
                if isinstance(pairs, str):
                    await interaction.followup.send(pairs, ephemeral=True)
                    return
                lead_inst, lead_card = pairs[0]
                player_line = [Fighter.from_instance(lead_inst, lead_card)]
                wild_card = await drop.draw_single_pokemon(session, drop_table_code="default")
        except SQLAlchemyError:
            _LOG.exception("next fight DB error for user %s", uid)
            await interaction.followup.send("Database error — try again.", ephemeral=True)
            return
        except (LookupError, RuntimeError) as exc:
            _LOG.exception("next fight wild roll failed for user %s", uid)
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        combat_id = f"pd:{uid}:{secrets.token_hex(4)}"
        try:
            wild = _fighter_from_catalog_card(wild_card)
            combat = DuelRuntime.from_lineups(
                lobby_id=combat_id,
                challenger_id=uid,
                opponent_id=0,
                bet=0,
                challenger_lineup=player_line,
                opponent_lineup=[wild],
                rng=self._rng,
            )
            self._combats[combat_id] = combat
            self._pve_meta[combat_id] = {
                "wild_name": wild_card.name,
                "wild_image": wild_card.image_large_url or wild_card.image_small_url,
                "wild_rarity_class_id": int(wild_card.rarity_class_id),
                "wild_hp": int(parse_hp(wild_card.hp)),
                "wild_max_dmg": _wild_max_attack_damage(wild_card),
            }

            winner = await self._pve_bot_step(combat_id)
            if winner is not None:
                await self._finish_pve(interaction, combat_id, winner, ctx=None)
                return

            meta = self._pve_meta.get(combat_id) or {}
            embeds = _pve_embeds(
                combat,
                wild_name=str(meta.get("wild_name") or "Wild"),
                wild_image=meta.get("wild_image") if isinstance(meta.get("wild_image"), str) else None,
                user_id=uid,
            )
            view = PveTurnView(self, combat_id, user_id=uid)
            file = await _render_vs_collage_file(
                left_name=combat.challenger_lineup[0].name,
                left_url=combat.challenger_lineup[0].image_large or combat.challenger_lineup[0].image_small,
                right_name=str(meta.get("wild_name") or "Wild"),
                right_url=(meta.get("wild_image") if isinstance(meta.get("wild_image"), str) else None)
                or (combat.opponent_lineup[0].image_large or combat.opponent_lineup[0].image_small),
            )
            if embeds is None:
                self._combats.pop(combat_id, None)
                self._pve_meta.pop(combat_id, None)
                await interaction.followup.send(
                    "Could not build battle view — try **`/pd`** again.", ephemeral=True
                )
                return

            sent = False
            if edit_message is not None:
                try:
                    if file is not None:
                        embeds[0].set_image(url="attachment://vs.png")
                        await edit_message.edit(embeds=embeds, view=view, attachments=[file])
                    else:
                        await edit_message.edit(embeds=embeds, view=view, attachments=[])
                    sent = True
                except discord.HTTPException:
                    _LOG.debug("edit_message failed, falling back to followup.send")

            if not sent:
                if file is not None:
                    embeds[0].set_image(url="attachment://vs.png")
                    await interaction.followup.send(embeds=embeds, view=view, file=file, ephemeral=False)
                else:
                    await interaction.followup.send(embeds=embeds, view=view, ephemeral=False)
        except Exception:
            _LOG.exception("next fight runtime error for user %s", uid)
            self._combats.pop(combat_id, None)
            self._pve_meta.pop(combat_id, None)
            try:
                await interaction.followup.send(
                    "Something went wrong during Poke-duel. Try **`/pd`** again.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def _resolve_pve_attack(self, interaction: discord.Interaction, combat_id: str, attack_index: int) -> None:
        combat = self._combats.get(combat_id)
        if combat is None:
            await interaction.response.send_message("This battle is over or missing.", ephemeral=True)
            return
        if interaction.user.id != combat.challenger_id:
            await interaction.response.send_message("This isn’t your battle.", ephemeral=True)
            return
        if combat.turn != "challenger":
            await interaction.response.send_message("Wait for the wild Pokémon to move.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            winner = combat.apply_attack(attack_index)
        except ValueError:
            await interaction.followup.send("Invalid attack.", ephemeral=True)
            return

        if winner is None:
            winner = await self._pve_bot_step(combat_id)

        if winner is not None:
            await self._finish_pve(interaction, combat_id, winner, ctx=None)
            return

        meta = self._pve_meta.get(combat_id) or {}
        embeds = _pve_embeds(
            combat,
            wild_name=str(meta.get("wild_name") or "Wild"),
            wild_image=meta.get("wild_image") if isinstance(meta.get("wild_image"), str) else None,
            user_id=interaction.user.id,
        )
        view = PveTurnView(self, combat_id, user_id=interaction.user.id)
        if embeds is None:
            await interaction.followup.send(
                "Battle state error — try **`/pd`** again.",
                ephemeral=True,
            )
            return
        file = await _render_vs_collage_file(
            left_name=combat.challenger_lineup[0].name,
            left_url=combat.challenger_lineup[0].image_large or combat.challenger_lineup[0].image_small,
            right_name=str(meta.get("wild_name") or "Wild"),
            right_url=(meta.get("wild_image") if isinstance(meta.get("wild_image"), str) else None)
            or (combat.opponent_lineup[0].image_large or combat.opponent_lineup[0].image_small),
        )
        if file is not None:
            embeds[0].set_image(url="attachment://vs.png")
        try:
            if interaction.message is not None:
                if file is not None:
                    await interaction.message.edit(embeds=embeds, view=view, attachments=[file])
                else:
                    await interaction.message.edit(embeds=embeds, view=view)
            else:
                if file is not None:
                    await interaction.followup.send(embeds=embeds, view=view, file=file, ephemeral=False)
                else:
                    await interaction.followup.send(embeds=embeds, view=view, ephemeral=False)
        except discord.HTTPException:
            _LOG.exception("pve attack UI refresh failed")
            try:
                if file is not None:
                    await interaction.followup.send(embeds=embeds, view=view, file=file, ephemeral=False)
                else:
                    await interaction.followup.send(embeds=embeds, view=view, ephemeral=False)
            except discord.HTTPException:
                await interaction.followup.send(
                    "Could not update the battle panel — try **`/pd`** again.",
                    ephemeral=True,
                )

    @commands.hybrid_group(name="duel", description="Pokémon TCG duels", invoke_without_command=True)
    async def duel_group(self, ctx: commands.Context) -> None:
        await ctx.send(
            "Use **`/duel challenge`** (slash) or **`c duel challenge @user <bet>`**.\n"
            "Save **1–6** Pokémon with **`/deck edit`** first.",
            ephemeral=_hybrid_ephemeral(ctx),
        )

    @commands.hybrid_command(
        name="pd",
        aliases=["pcpd"],
        description="Poke-duel: instantly fight a wild Pokémon for Pokedollars. Chat: pcpd",
    )
    async def poke_duel(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        uid = ctx.author.id
        drop = DropService(rng=self._rng)

        if self._user_busy(uid):
            await ctx.send("You’re already in a duel/battle.", ephemeral=False)
            return

        try:
            async with self.bot.async_session_factory() as session:
                ids = await get_saved_instance_ids(session, uid)
                if not ids:
                    await ctx.send("You need a combat deck first — use **`/deck edit`**.", ephemeral=False)
                    return
                pairs = await load_fighters_ordered(session, uid, ids)
                if isinstance(pairs, str):
                    await ctx.send(pairs, ephemeral=False)
                    return

                lead_inst, lead_card = pairs[0]
                player_line = [Fighter.from_instance(lead_inst, lead_card)]

                wild_card = await drop.draw_single_pokemon(session, drop_table_code="default")
        except SQLAlchemyError:
            _LOG.exception("poke duel setup DB error for user %s", uid)
            await ctx.send("Database error — try again.", ephemeral=False)
            return
        except (LookupError, RuntimeError) as exc:
            _LOG.exception("poke duel wild roll failed for user %s", uid)
            await ctx.send(str(exc), ephemeral=False)
            return

        combat_id = f"pd:{uid}:{secrets.token_hex(4)}"
        try:
            wild = _fighter_from_catalog_card(wild_card)
            combat = DuelRuntime.from_lineups(
                lobby_id=combat_id,
                challenger_id=uid,
                opponent_id=0,
                bet=0,
                challenger_lineup=player_line,
                opponent_lineup=[wild],
                rng=self._rng,
            )
            self._combats[combat_id] = combat
            self._pve_meta[combat_id] = {
                "wild_name": wild_card.name,
                "wild_image": wild_card.image_large_url or wild_card.image_small_url,
                "wild_rarity_class_id": int(wild_card.rarity_class_id),
                "wild_hp": int(parse_hp(wild_card.hp)),
                "wild_max_dmg": _wild_max_attack_damage(wild_card),
            }

            # If the bot goes first, play its turn(s) immediately.
            winner = await self._pve_bot_step(combat_id)
            if winner is not None:
                await self._finish_pve(ctx.interaction, combat_id, winner, ctx=ctx)
                return

            meta = self._pve_meta.get(combat_id) or {}
            embeds = _pve_embeds(
                combat,
                wild_name=str(meta.get("wild_name") or "Wild"),
                wild_image=meta.get("wild_image") if isinstance(meta.get("wild_image"), str) else None,
                user_id=uid,
            )
            view = PveTurnView(self, combat_id, user_id=uid)
            file = await _render_vs_collage_file(
                left_name=combat.challenger_lineup[0].name,
                left_url=combat.challenger_lineup[0].image_large or combat.challenger_lineup[0].image_small,
                right_name=str(meta.get("wild_name") or "Wild"),
                right_url=(meta.get("wild_image") if isinstance(meta.get("wild_image"), str) else None)
                or (combat.opponent_lineup[0].image_large or combat.opponent_lineup[0].image_small),
            )
            if embeds is None:
                self._combats.pop(combat_id, None)
                self._pve_meta.pop(combat_id, None)
                await ctx.send(
                    "Could not build battle view — try **`/pd`** / **`pcpd`** again.",
                    ephemeral=False,
                )
                return
            if file is not None:
                embeds[0].set_image(url="attachment://vs.png")
                await ctx.send(embeds=embeds, view=view, file=file, ephemeral=False)
            else:
                await ctx.send(embeds=embeds, view=view, ephemeral=False)
        except Exception:
            _LOG.exception("poke duel runtime/UI error for user %s", uid)
            self._combats.pop(combat_id, None)
            self._pve_meta.pop(combat_id, None)
            try:
                await ctx.send(
                    "Something went wrong during Poke-duel. Try again.",
                    ephemeral=False,
                )
            except discord.HTTPException:
                pass

    @duel_group.command(name="challenge", aliases=["chal", "c", "dc"])
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
            f"**`/deck edit`** — interactive bench (**{MIN_DECK}–{MAX_DECK}** Pokémon): pick a slot, "
            "reply with a **Card ID**, **Clear slot**, or **Done**.\n"
            "**`/deck view`** — saved bench."
            + _deck_web_footer(),
            ephemeral=_hybrid_ephemeral(ctx),
        )

    @deck_group.command(name="edit", aliases=["e"])
    async def deck_edit(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.defer(ephemeral=False)
        ephe = _hybrid_ephemeral(ctx)
        uid = ctx.author.id
        self._stop_deck_edit_listener(uid)
        try:
            async with self.bot.async_session_factory() as session:
                slots = await load_deck_slots_padded(session, uid)
        except SQLAlchemyError:
            _LOG.exception("deck edit load %s", uid)
            await ctx.send("Could not load your deck.", ephemeral=ephe)
            return
        view = DeckEditView(self, uid, slots)
        try:
            async with self.bot.async_session_factory() as session:
                embed = await view.build_embed(session)
                await view.rebuild_items(session)
        except SQLAlchemyError:
            _LOG.exception("deck edit init %s", uid)
            await ctx.send("Could not open deck editor.", ephemeral=ephe)
            return
        msg = await ctx.send(embed=embed, view=view, ephemeral=ephe)
        view.panel_message = msg
        self._deck_edit_tasks[uid] = asyncio.create_task(self._deck_edit_listener(view))

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
                    await ctx.send("No deck saved yet — use **`/deck edit`**.", ephemeral=ephe)
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
