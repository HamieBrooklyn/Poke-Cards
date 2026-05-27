"""Advanced website duel runtime: decks + energy/items draw stacks + multi-action turns."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from poke_pon_bot.services.duel_engine import (
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


ENERGY_TYPES = ("Fire", "Water", "Grass", "Lightning", "Psychic", "Fighting", "Darkness", "Metal", "Colorless")


def _clamp_int(x: Any, lo: int, hi: int) -> int:
    try:
        n = int(x)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


def _rng_for_duel(duel_id: int) -> random.Random:
    return random.Random(int(duel_id) * 1009 + 17)


def _init_energy_deck(rng: random.Random, n: int = 60) -> list[str]:
    # Biased slightly toward Colorless to make early turns playable.
    pool = list(ENERGY_TYPES)
    weights = [10, 10, 10, 10, 9, 9, 7, 7, 18]
    out = []
    for _ in range(n):
        out.append(rng.choices(pool, weights=weights, k=1)[0])
    return out


ITEM_CATALOG: dict[str, dict[str, Any]] = {
    "potion": {"name": "Potion", "effect": {"type": "heal", "amount": 20}},
    "super_potion": {"name": "Super Potion", "effect": {"type": "heal", "amount": 50}},
    "x_attack": {"name": "X Attack", "effect": {"type": "buff_damage", "amount": 10, "turns": 1}},
    "shield": {"name": "Shield", "effect": {"type": "shield", "amount": 15, "hits": 1}},
    "revive": {"name": "Revive", "effect": {"type": "revive", "hp": 30}},
}


def _init_item_deck(rng: random.Random, n: int = 40) -> list[str]:
    keys = list(ITEM_CATALOG.keys())
    weights = [16, 8, 10, 10, 6]
    out = []
    for _ in range(n):
        out.append(rng.choices(keys, weights=weights, k=1)[0])
    return out


def _energy_cost_can_pay(pool: dict[str, int], cost: list[str] | None) -> bool:
    if not cost:
        return True
    need: dict[str, int] = {}
    colorless = 0
    for c in cost:
        s = str(c or "").strip()
        if not s:
            continue
        if s.lower() == "colorless":
            colorless += 1
        else:
            need[s] = need.get(s, 0) + 1
    # pay typed
    for t, n in need.items():
        if int(pool.get(t, 0)) < n:
            return False
    remaining = sum(int(v) for v in pool.values()) - sum(need.values())
    return remaining >= colorless


def _energy_cost_pay(pool: dict[str, int], cost: list[str] | None) -> dict[str, int]:
    if not cost:
        return pool
    need: dict[str, int] = {}
    colorless = 0
    for c in cost:
        s = str(c or "").strip()
        if not s:
            continue
        if s.lower() == "colorless":
            colorless += 1
        else:
            need[s] = need.get(s, 0) + 1
    # spend typed
    for t, n in need.items():
        pool[t] = int(pool.get(t, 0)) - int(n)
        if pool[t] <= 0:
            pool.pop(t, None)
    # spend colorless from highest counts
    while colorless > 0:
        if not pool:
            break
        t = max(pool.keys(), key=lambda k: int(pool.get(k, 0)))
        pool[t] = int(pool[t]) - 1
        if pool[t] <= 0:
            pool.pop(t, None)
        colorless -= 1
    return pool


@dataclass
class DuelRuntimeAdvanced:
    duel_id: int
    initiator_id: int
    partner_id: int
    bet_currency: str
    bet_amount: int

    turn: int
    draws_remaining: int = 2

    # per user
    energy_pool: dict[str, dict[str, int]] = field(default_factory=dict)
    items: dict[str, list[str]] = field(default_factory=dict)

    # buffs/shields
    buffs: dict[str, dict[str, Any]] = field(default_factory=dict)  # key: "{uid}:{slot}" -> meta
    shields: dict[str, dict[str, Any]] = field(default_factory=dict)

    # deck: list of fighters-serialized dicts, with hp fields
    lineups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # hidden stacks (server only)
    energy_deck: list[str] = field(default_factory=list)
    item_deck: list[str] = field(default_factory=list)

    log: list[dict[str, Any]] = field(default_factory=list)
    winner_id: int | None = None
    last_event: dict[str, Any] | None = None

    @classmethod
    def from_state(
        cls,
        state: dict[str, Any],
        *,
        duel_id: int,
        initiator_id: int,
        partner_id: int,
        bet_currency: str,
        bet_amount: int,
    ) -> "DuelRuntimeAdvanced":
        rng = _rng_for_duel(duel_id)
        base_turn = int(state.get("turn") or 0) or (initiator_id if rng.random() < 0.5 else partner_id)
        energy_deck = state.get("_energy_deck")
        item_deck = state.get("_item_deck")
        if not isinstance(energy_deck, list):
            energy_deck = _init_energy_deck(rng)
        if not isinstance(item_deck, list):
            item_deck = _init_item_deck(rng)
        rt = cls(
            duel_id=int(duel_id),
            initiator_id=int(initiator_id),
            partner_id=int(partner_id),
            bet_currency=str(bet_currency),
            bet_amount=int(bet_amount),
            turn=int(base_turn),
            draws_remaining=_clamp_int(state.get("draws_remaining"), 0, 2),
            energy_pool=state.get("energy_pool") if isinstance(state.get("energy_pool"), dict) else {},
            items=state.get("items") if isinstance(state.get("items"), dict) else {},
            buffs=state.get("buffs") if isinstance(state.get("buffs"), dict) else {},
            shields=state.get("shields") if isinstance(state.get("shields"), dict) else {},
            lineups=state.get("lineups") if isinstance(state.get("lineups"), dict) else {},
            energy_deck=list(energy_deck),
            item_deck=list(item_deck),
            log=state.get("log") if isinstance(state.get("log"), list) else [],
            winner_id=int(state["winner"]) if state.get("winner") is not None else None,
        )
        # ensure keys
        for uid in (initiator_id, partner_id):
            rt.energy_pool.setdefault(str(uid), {})
            rt.items.setdefault(str(uid), [])
            rt.lineups.setdefault(str(uid), [])
        return rt

    def to_state(self) -> dict[str, Any]:
        return {
            "duel_id": int(self.duel_id),
            "players": [int(self.initiator_id), int(self.partner_id)],
            "turn": int(self.turn),
            "draws_remaining": int(self.draws_remaining),
            "energy_pool": self.energy_pool,
            "items": self.items,
            "buffs": self.buffs,
            "shields": self.shields,
            "lineups": self.lineups,
            "stacks": {
                "energy": {"remaining": len(self.energy_deck)},
                "item": {"remaining": len(self.item_deck)},
            },
            "log": self.log,
            "winner": self.winner_id,
            # hidden stacks persisted for reconnect determinism
            "_energy_deck": self.energy_deck,
            "_item_deck": self.item_deck,
        }

    def _assert_actor_turn(self, actor_id: int) -> None:
        if self.winner_id is not None:
            raise ValueError("Duel is already finished.")
        if int(actor_id) != int(self.turn):
            raise ValueError("Not your turn.")

    def other_id(self, uid: int) -> int:
        return self.partner_id if int(uid) == int(self.initiator_id) else self.initiator_id

    def append_log_event(self, ev: dict[str, Any]) -> None:
        self.last_event = ev
        self.log.append(ev)

    def apply_draw(self, *, actor_id: int, source: str, count: int) -> dict[str, Any]:
        self._assert_actor_turn(actor_id)
        count = _clamp_int(count, 1, 2)
        if count > self.draws_remaining:
            raise ValueError("Not enough draws remaining.")
        src = str(source or "").strip().lower()
        drawn: list[str] = []
        if src == "energy":
            for _ in range(count):
                if not self.energy_deck:
                    break
                drawn.append(self.energy_deck.pop())
            pool = self.energy_pool[str(actor_id)]
            for e in drawn:
                pool[e] = int(pool.get(e, 0)) + 1
        elif src == "item":
            for _ in range(count):
                if not self.item_deck:
                    break
                drawn.append(self.item_deck.pop())
            self.items[str(actor_id)].extend(drawn)
        else:
            raise ValueError("Invalid draw source.")

        self.draws_remaining -= count
        return {"type": "draw", "actor": int(actor_id), "from": src, "count": count, "drawn": drawn}

    def _slot_key(self, uid: int, slot: int) -> str:
        return f"{int(uid)}:{int(slot)}"

    def _alive_slots(self, uid: int) -> list[dict[str, Any]]:
        lineup = self.lineups.get(str(uid)) or []
        return [x for x in lineup if int(x.get("current_hp") or x.get("max_hp") or 1) > 0]

    def _ensure_hp_fields(self, uid: int) -> None:
        lineup = self.lineups.get(str(uid)) or []
        for row in lineup:
            if "max_hp" not in row:
                row["max_hp"] = int(row.get("hp") or row.get("max_hp") or 1)
            if "current_hp" not in row:
                row["current_hp"] = int(row.get("max_hp") or 1)

    def apply_item(self, *, actor_id: int, item_id: str, target_slot: int) -> dict[str, Any]:
        self._assert_actor_turn(actor_id)
        inv = self.items.get(str(actor_id)) or []
        if item_id not in inv:
            raise ValueError("You don't have that item.")
        item = ITEM_CATALOG.get(item_id)
        if not item:
            raise ValueError("Unknown item.")

        self._ensure_hp_fields(actor_id)
        lineup = self.lineups.get(str(actor_id)) or []
        if target_slot < 0 or target_slot >= len(lineup):
            raise ValueError("Invalid target.")
        tgt = lineup[target_slot]
        eff = item["effect"]
        et = eff.get("type")

        inv.remove(item_id)

        if et == "heal":
            amt = int(eff.get("amount") or 0)
            before = int(tgt.get("current_hp") or 0)
            mx = int(tgt.get("max_hp") or before or 1)
            tgt["current_hp"] = min(mx, before + amt)
            return {"type": "item", "actor": int(actor_id), "item": item_id, "target_slot": int(target_slot), "heal": int(tgt["current_hp"]) - before}
        if et == "buff_damage":
            amt = int(eff.get("amount") or 0)
            turns = int(eff.get("turns") or 1)
            self.buffs[self._slot_key(actor_id, target_slot)] = {"amount": amt, "turns": turns}
            return {"type": "item", "actor": int(actor_id), "item": item_id, "target_slot": int(target_slot), "buff_damage": amt, "turns": turns}
        if et == "shield":
            amt = int(eff.get("amount") or 0)
            hits = int(eff.get("hits") or 1)
            self.shields[self._slot_key(actor_id, target_slot)] = {"amount": amt, "hits": hits}
            return {"type": "item", "actor": int(actor_id), "item": item_id, "target_slot": int(target_slot), "shield": amt, "hits": hits}
        if et == "revive":
            # revive first defeated Pokémon if any, else no-op refund not supported (kept simple).
            hp = int(eff.get("hp") or 10)
            for idx, row in enumerate(lineup):
                if int(row.get("current_hp") or 0) <= 0:
                    mx = int(row.get("max_hp") or hp)
                    row["current_hp"] = min(mx, hp)
                    return {"type": "item", "actor": int(actor_id), "item": item_id, "target_slot": idx, "revive_hp": int(row["current_hp"])}
            return {"type": "item", "actor": int(actor_id), "item": item_id, "target_slot": int(target_slot), "revive_hp": 0}

        raise ValueError("Unsupported item.")

    def apply_attack(
        self,
        *,
        actor_id: int,
        attacker_slot: int,
        attack_index: int,
        defender_slot: int,
    ) -> dict[str, Any]:
        self._assert_actor_turn(actor_id)
        enemy_id = self.other_id(actor_id)
        self._ensure_hp_fields(actor_id)
        self._ensure_hp_fields(enemy_id)

        my_line = self.lineups.get(str(actor_id)) or []
        en_line = self.lineups.get(str(enemy_id)) or []
        if attacker_slot < 0 or attacker_slot >= len(my_line):
            raise ValueError("Invalid attacker.")
        if defender_slot < 0 or defender_slot >= len(en_line):
            raise ValueError("Invalid defender.")
        atk_row = my_line[attacker_slot]
        def_row = en_line[defender_slot]
        if int(atk_row.get("current_hp") or 0) <= 0:
            raise ValueError("That Pokémon is defeated.")
        if int(def_row.get("current_hp") or 0) <= 0:
            raise ValueError("That target is already defeated.")

        attacks = atk_row.get("attacks")
        if not isinstance(attacks, list) or not attacks:
            attacks = [{"name": "Struggle", "damage_int": 10, "cost": None}]
        if attack_index < 0 or attack_index >= len(attacks):
            raise ValueError("Invalid attack.")
        mv = attacks[attack_index]
        cost = mv.get("cost")
        if not isinstance(cost, list):
            cost = None

        pool = self.energy_pool.get(str(actor_id)) or {}
        if not _energy_cost_can_pay(pool, cost):
            raise ValueError("Not enough energy for that move.")
        _energy_cost_pay(pool, cost)
        self.energy_pool[str(actor_id)] = pool

        base = int(mv.get("damage_int") or 0)
        # apply buff if present
        buff = self.buffs.get(self._slot_key(actor_id, attacker_slot))
        if isinstance(buff, dict):
            base += int(buff.get("amount") or 0)

        atk_types = tuple(def_row.get("types") or ("Normal",))
        def_types = tuple(def_row.get("types") or ("Normal",))
        atk_chart = move_attacking_chart_type(cost)
        factor = combined_type_factor(atk_chart, def_types)
        dmg = scaled_damage(base, factor)

        # shield on defender
        sh_key = self._slot_key(enemy_id, defender_slot)
        shield = self.shields.get(sh_key)
        reduced = 0
        if isinstance(shield, dict) and int(shield.get("hits") or 0) > 0:
            red = int(shield.get("amount") or 0)
            reduced = min(dmg, red)
            dmg = max(0, dmg - red)
            shield["hits"] = int(shield.get("hits") or 0) - 1
            if int(shield["hits"]) <= 0:
                self.shields.pop(sh_key, None)
            else:
                self.shields[sh_key] = shield

        before = int(def_row.get("current_hp") or 0)
        def_row["current_hp"] = before - dmg

        ko = False
        if int(def_row["current_hp"]) <= 0:
            def_row["current_hp"] = 0
            ko = True

        # win check: all enemy defeated
        if all(int(x.get("current_hp") or 0) <= 0 for x in en_line):
            self.winner_id = int(actor_id)

        return {
            "type": "attack",
            "actor": int(actor_id),
            "attacker_slot": int(attacker_slot),
            "defender_slot": int(defender_slot),
            "move": str(mv.get("name") or "Attack"),
            "damage": int(dmg),
            "reduced": int(reduced),
            "factor": float(factor),
            "ko": ko,
            "winner": int(self.winner_id) if self.winner_id is not None else None,
        }

    def end_turn(self, *, actor_id: int) -> dict[str, Any]:
        self._assert_actor_turn(actor_id)
        self.turn = self.other_id(actor_id)
        self.draws_remaining = 2
        # decay buffs
        to_del = []
        for k, v in (self.buffs or {}).items():
            if not isinstance(v, dict):
                continue
            v["turns"] = int(v.get("turns") or 0) - 1
            if int(v["turns"]) <= 0:
                to_del.append(k)
        for k in to_del:
            self.buffs.pop(k, None)
        return {"type": "end_turn", "actor": int(actor_id), "next": int(self.turn)}

    def surrender(self, *, actor_id: int) -> dict[str, Any]:
        self._assert_actor_turn(actor_id)
        self.winner_id = self.other_id(actor_id)
        return {"type": "surrender", "actor": int(actor_id), "winner": int(self.winner_id)}

