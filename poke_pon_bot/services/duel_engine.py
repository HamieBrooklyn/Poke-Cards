"""Turn-based duel resolution from catalog card stats."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Literal

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.type_effectiveness import (
    ansi_colored_battle_line,
    combined_type_factor,
    defense_types_from_card_types,
    move_attacking_chart_type,
    scaled_damage,
    tcg_display_name,
)

Side = Literal["challenger", "opponent"]


def parse_hp(hp_raw: str | None) -> int:
    if not hp_raw:
        return 0
    s = str(hp_raw).strip()
    digits: list[str] = []
    for ch in s:
        if ch.isdigit():
            digits.append(ch)
        elif digits:
            break
    return int("".join(digits)) if digits else 0


def parse_attack_damage(damage_val: object) -> int:
    if damage_val is None:
        return 0
    s = str(damage_val).strip()
    m = re.match(r"^(\d+)", s)
    return int(m.group(1)) if m else 0


def normalized_attacks(card: Card) -> list[dict]:
    """Attack dicts with ``name``, ``damage_int``, optional ``text``."""
    out: list[dict] = []
    raw = card.attacks
    if isinstance(raw, list):
        for a in raw:
            if not isinstance(a, dict):
                continue
            nm = str(a.get("name") or "Attack").strip() or "Attack"
            dmg = parse_attack_damage(a.get("damage"))
            tx = a.get("text")
            text = str(tx).strip() if tx is not None and str(tx).strip() else None
            cost_raw = a.get("cost")
            cost: list[str] | None = None
            if isinstance(cost_raw, list) and cost_raw:
                cost = [str(c).strip() for c in cost_raw if c is not None and str(c).strip()]
                if not cost:
                    cost = None
            out.append({"name": nm, "damage_int": max(0, dmg), "text": text, "cost": cost})
    if not out:
        out.append({"name": "Struggle", "damage_int": 10, "text": None, "cost": None})
    return out


@dataclass
class Fighter:
    instance_id: int
    public_id: str
    name: str
    image_small: str | None
    image_large: str | None
    max_hp: int
    current_hp: int
    types: tuple[str, ...] = ("Normal",)
    attacks: list[dict] = field(default_factory=list)

    @classmethod
    def from_instance(cls, inst: UserCardInstance, card: Card) -> Fighter:
        hp = max(1, parse_hp(card.hp))
        sm = card.image_small_url or card.image_large_url
        lg = card.image_large_url or card.image_small_url
        deft = defense_types_from_card_types(card.tcg_types, supertype=card.supertype)
        return cls(
            instance_id=inst.id,
            public_id=inst.public_id,
            name=card.name,
            image_small=sm,
            image_large=lg,
            max_hp=hp,
            current_hp=hp,
            types=deft,
            attacks=normalized_attacks(card),
        )


def best_attack_index(attacker: Fighter, defender: Fighter) -> int:
    """Pick the move index that deals the most **type-adjusted** damage (wild AI)."""
    if not attacker.attacks:
        return 0
    best_i = 0
    best_eff = -1
    for k, mv in enumerate(attacker.attacks):
        cost = mv.get("cost")
        if not isinstance(cost, list):
            cost = None
        atk_t = move_attacking_chart_type(cost)
        fac = combined_type_factor(atk_t, defender.types)
        eff = scaled_damage(int(mv["damage_int"]), fac)
        if eff > best_eff:
            best_eff = eff
            best_i = k
    return best_i


@dataclass
class DuelRuntime:
    lobby_id: str
    challenger_id: int
    opponent_id: int
    bet: int
    challenger_lineup: list[Fighter]
    opponent_lineup: list[Fighter]
    turn: Side
    log_lines: list[str] = field(default_factory=list)

    @classmethod
    def from_lineups(
        cls,
        *,
        lobby_id: str,
        challenger_id: int,
        opponent_id: int,
        bet: int,
        challenger_lineup: list[Fighter],
        opponent_lineup: list[Fighter],
        rng: random.Random,
    ) -> DuelRuntime:
        first: Side = "challenger" if rng.random() < 0.5 else "opponent"
        r = cls(
            lobby_id=lobby_id,
            challenger_id=challenger_id,
            opponent_id=opponent_id,
            bet=bet,
            challenger_lineup=challenger_lineup,
            opponent_lineup=opponent_lineup,
            turn=first,
        )
        r.log_lines.append(
            f"Battle start! {'Challenger' if first == 'challenger' else 'Opponent'} goes first.\n{r._field_summary()}"
        )
        return r

    def _field_summary(self) -> str:
        ca = self.challenger_lineup[0] if self.challenger_lineup else None
        oa = self.opponent_lineup[0] if self.opponent_lineup else None
        if ca is None or oa is None:
            return ""
        return (
            f"Active: {ca.name} ({ca.current_hp} / {ca.max_hp} HP) vs "
            f"{oa.name} ({oa.current_hp} / {oa.max_hp} HP)\n"
            f"Bench: {max(0, len(self.challenger_lineup) - 1)} vs {max(0, len(self.opponent_lineup) - 1)}"
        )

    def current_turn_user_id(self) -> int:
        return self.challenger_id if self.turn == "challenger" else self.opponent_id

    def apply_attack(self, attack_index: int) -> int | None:
        """
        Apply the attacker’s move. Appends to ``log_lines``.
        Returns **winner discord user id** when the duel ends, else ``None``.
        """
        if self.turn == "challenger":
            atk_line, def_line = self.challenger_lineup, self.opponent_lineup
            atk_name_side = "Challenger"
        else:
            atk_line, def_line = self.opponent_lineup, self.challenger_lineup
            atk_name_side = "Opponent"

        if not atk_line or not def_line:
            return None

        attacker = atk_line[0]
        defender = def_line[0]
        attacks = attacker.attacks
        if attack_index < 0 or attack_index >= len(attacks):
            raise ValueError("Invalid attack index")
        move = attacks[attack_index]
        name = str(move["name"])
        base_dmg = int(move["damage_int"])
        cost = move.get("cost")
        if not isinstance(cost, list):
            cost = None
        atk_chart = move_attacking_chart_type(cost)
        def_types = defender.types
        factor = combined_type_factor(atk_chart, def_types)
        final_dmg = scaled_damage(base_dmg, factor)
        defender.current_hp -= final_dmg

        move_label = tcg_display_name(atk_chart)
        def_label = " / ".join(tcg_display_name(t) for t in def_types)
        fac_disp = str(int(factor)) if float(factor) == int(factor) else f"{factor:g}"

        main_line = (
            f"{atk_name_side}'s {attacker.name} uses {name} — {final_dmg} damage to {defender.name} "
            f"({max(0, defender.current_hp)}/{defender.max_hp} HP). "
            f"Type: {move_label} vs {def_label} · card base {base_dmg} ×{fac_disp}"
        )

        extra: list[str] = []
        winner: int | None = None
        if defender.current_hp <= 0:
            extra.append(f"{defender.name} is Knocked Out!")
            def_line.pop(0)
            if not def_line:
                winner = self.challenger_id if self.turn == "challenger" else self.opponent_id
                extra.append("No Pokémon left — duel over!")
            else:
                nxt = def_line[0]
                extra.append(f"{nxt.name} is sent out ({nxt.current_hp} / {nxt.max_hp} HP).")

        body = main_line
        if extra:
            body = main_line + "\n" + "\n".join(extra)
        chunk = ansi_colored_battle_line(body, type_factor=factor)
        self.log_lines.append(chunk)

        self._last_move_meta = {
            "attacker": attacker.name,
            "defender": defender.name,
            "move": name,
            "damage": final_dmg,
            "factor": float(factor),
        }
        if winner is not None:
            return winner

        self.turn = "opponent" if self.turn == "challenger" else "challenger"
        self.log_lines.append(self._field_summary())
        return None
