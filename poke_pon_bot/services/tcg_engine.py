"""Authoritative physical TCG state machine, separate from the legacy duel engine.

Only catalog-certified card effects are executable. Unknown effects fail closed.
State is JSON serializable; store it server-side and expose only ``project_state``.
Random outcomes are stored in the resulting state. Never persist/expose RNG seeds.
Rules baseline: physical TCG rulebook + Professor Program February 2026 updates.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from itertools import product
import random
import re
from typing import Any

from poke_pon_bot.services.tcg_catalog import validate_deck

RULES_VERSION = "physical-tcg-2026-02-20-v1"
RULES_SOURCES = [
    "https://tcg.pokemon.com/en-us/learn/",
    "https://professorprogram.pokemon.com/news/11473085",
]


class RuleError(ValueError):
    """An action is illegal; the supplied state has not been mutated."""


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise RuleError(message)


def _rng(rng=None):
    return rng if rng is not None else random.SystemRandom()


def _other(state: dict, player: str) -> str:
    return next(p for p in state["players"] if p != player)


def _basic(card: dict) -> bool:
    return card.get("supertype") == "Pokémon" and "Basic" in card.get("subtypes", [])


def _basic_energy(card: dict) -> bool:
    return card.get("supertype") == "Energy" and "Basic" in card.get("subtypes", [])


def _log(state: dict, message: str) -> None:
    state["log"].append({"message": message, "turn": state["turn_number"]})


def _flip(state: dict, rng, purpose: str) -> bool:
    heads = bool(rng.getrandbits(1))
    _log(state, f"{purpose}: {'heads' if heads else 'tails'}.")
    return heads


def _draw(state: dict, player: str, count: int) -> None:
    board = state["board"][player]
    # Empty deck loses only at the mandatory turn draw, not effect draws.
    for _ in range(min(count, len(board["deck"]))):
        board["hand"].append(board["deck"].pop())


def _instance(card: dict, rng) -> dict:
    return {"uid": f"c{rng.getrandbits(128):032x}", "card": deepcopy(card)}


def _in_play(instance: dict, turn: int) -> dict:
    return {**instance, "attachments": [], "evolution": [], "damage": 0,
            "statuses": [], "entered_turn": turn, "evolved_turn": -1,
            "ability_used_turn": -1, "abilities_used": {}}


def _pokemon(board: dict, uid: str) -> dict:
    for pokemon in [board["active"], *board["bench"]]:
        if pokemon and pokemon["uid"] == uid:
            return pokemon
    raise RuleError("Select one of your Pokémon in play.")


def _find(zone: list, uid: str) -> dict:
    for card in zone:
        if card["uid"] == uid:
            return card
    raise RuleError("That card is not in the required zone.")


def _pop(zone: list, uid: str) -> dict:
    card = _find(zone, uid)
    zone.remove(card)
    return card


def _effects(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if value.get("kind") == "sequence":
            return value.get("effects", [])
        return [value]
    raise RuleError("Malformed card effect.")


ATTACK_EFFECTS = {"damage", "flip_damage", "flip_bonus", "flip_fail", "condition",
                  "flip_condition", "draw", "heal_self", "recoil", "discard_self_energy"}
TRAINER_EFFECTS = {"draw", "discard_hand_draw", "heal", "switch", "search_basic",
                   "search_energy", "gust", "shuffle_hand_draw", "tool", "stadium"}


ABILITY_EFFECTS = {"draw", "draw_to", "heal_self", "attach_energy"}


def _validate_modifier(effect: dict) -> None:
    kind = effect.get("kind")
    allowed = {"kind", "applies_to", "retreat_reduction"}
    if kind == "tool":
        allowed |= {"hp_bonus", "damage_bonus", "retreat_free"}
        numeric = {"hp_bonus", "damage_bonus", "retreat_reduction"}
    elif kind == "stadium":
        allowed |= {"heal", "heal_types", "heal_all"}
        numeric = {"heal", "retreat_reduction"}
    else:
        raise RuleError("Unknown persistent card effect.")
    _require(not set(effect) - allowed, "Unsupported persistent effect condition.")
    _require(any(effect.get(k) for k in numeric | {"retreat_free"}), "Persistent card has no implemented effect.")
    for key in numeric & set(effect):
        _require(type(effect[key]) is int and effect[key] > 0, "A modifier must be a positive whole number.")
    for key in {"retreat_free", "heal_all"} & set(effect):
        _require(type(effect[key]) is bool, "Modifier flags must be boolean.")
    applies = effect.get("applies_to", {})
    _require(isinstance(applies, dict) and not set(applies) - {"subtypes", "exclude_subtypes"},
             "Unsupported Pokémon modifier filter.")
    for values in applies.values():
        _require(isinstance(values, list) and all(isinstance(v, str) for v in values), "Malformed Pokémon filter.")
    if "heal_types" in effect:
        _require(isinstance(effect["heal_types"], list) and all(isinstance(v, str) for v in effect["heal_types"]),
                 "Malformed Stadium healing filter.")


def _validate_ability(ability: dict) -> None:
    _require(ability.get("type") == "Ability", "Only explicitly implemented Abilities are supported.")
    effect = ability.get("effect", {})
    kind = effect.get("kind")
    _require(kind in ABILITY_EFFECTS, "Ability requires an implemented handler.")
    fields = {"kind", "once_per_turn", "active_only"}
    required = {"draw": "count", "draw_to": "hand_size", "heal_self": "amount"}
    if kind in required:
        fields.add(required[kind])
        _require(effect.get("once_per_turn") is True, "This Ability frequency is not implemented.")
        _require(type(effect.get(required[kind])) is int and effect[required[kind]] > 0,
                 "Ability amounts must be positive whole numbers.")
    else:
        fields |= {"energy_type", "target_type", "basic_only", "benched_only"}
        _require(type(effect.get("once_per_turn")) is bool and effect.get("basic_only") is True,
                 "Only Basic Energy Ability attachments with explicit frequency are implemented.")
        _require(isinstance(effect.get("energy_type"), str) and bool(effect["energy_type"]),
                 "Ability must identify an Energy type.")
    _require(not set(effect) - fields, "Unsupported Ability condition.")
    for key in {"active_only", "benched_only"} & set(effect):
        _require(type(effect[key]) is bool, "Ability flags must be boolean.")


def _matches(pokemon: dict, filters: dict | None) -> bool:
    filters = filters or {}
    subtypes = set(pokemon["card"].get("subtypes", []))
    return (not filters.get("subtypes") or bool(subtypes & set(filters["subtypes"]))) and not bool(subtypes & set(filters.get("exclude_subtypes", [])))


def _tools(pokemon: dict) -> list[dict]:
    return [c for c in pokemon["attachments"] if "Pokémon Tool" in c["card"].get("subtypes", [])]


def _tool_modifiers(pokemon: dict):
    for tool in _tools(pokemon):
        effect = tool["card"].get("effect", {})
        _validate_modifier(effect)
        if _matches(pokemon, effect.get("applies_to")):
            yield effect


def _effective_hp(pokemon: dict) -> int:
    return int(pokemon["card"]["hp"]) + sum(e.get("hp_bonus", 0) for e in _tool_modifiers(pokemon))


def _retreat_cost(state: dict, pokemon: dict) -> int:
    modifiers = list(_tool_modifiers(pokemon))
    if any(e.get("retreat_free") for e in modifiers):
        return 0
    reduction = sum(e.get("retreat_reduction", 0) for e in modifiers)
    stadium = state.get("stadium")
    if stadium:
        effect = stadium["card"].get("effect", {})
        _validate_modifier(effect)
        if _matches(pokemon, effect.get("applies_to")):
            reduction += effect.get("retreat_reduction", 0)
    return max(0, len(pokemon["card"].get("retreat_cost", [])) - reduction)


def _validate_card(card: dict) -> None:
    _require(card.get("supported") is True, f"{card.get('name', 'Card')} has unsupported rules.")
    supertype = card.get("supertype")
    if supertype == "Pokémon":
        for ability in card.get("abilities", []):
            _validate_ability(ability)
        _require(int(card.get("hp", 0)) > 0, "Pokémon must have valid HP.")
        for attack in card.get("attacks", []):
            effects = _effects(attack.get("effect"))
            _require(not attack.get("text") or effects, "Attack text has no executable effect.")
            _require(all(e.get("kind") in ATTACK_EFFECTS for e in effects), "Unsupported attack effect.")
            damage = str(attack.get("damage", "") or "")
            _require(bool(re.fullmatch(r"\d*[+×x*]?", damage)), "Unsupported damage expression.")
            if damage and not damage.isdigit():
                kinds = {e.get("kind") for e in effects}
                _require(bool(kinds & {"flip_damage", "flip_bonus"}), "Variable damage has no handler.")
    elif supertype == "Trainer":
        _require(card.get("effect", {}).get("kind") in TRAINER_EFFECTS, "Unsupported Trainer effect.")
        subtypes = set(card.get("subtypes", []))
        _require("Technical Machine" not in subtypes, "Technical Machine effects require an implemented handler.")
        effect_kind = card.get("effect", {}).get("kind")
        _require(("Pokémon Tool" in subtypes) == (effect_kind == "tool"), "Tool subtype and effect disagree.")
        _require(("Stadium" in subtypes) == (effect_kind == "stadium"), "Stadium subtype and effect disagree.")
        if effect_kind in {"tool", "stadium"}:
            _validate_modifier(card["effect"])
    elif supertype == "Energy":
        _require(card.get("effect", {}).get("kind") in {"basic_energy", "double_colorless"},
                 "Unsupported Energy effect.")
    else:
        raise RuleError("This catalog entry is not a playable TCG card.")


def new_game(decks: dict[str, list[dict]], players: list[str], rng=None) -> dict:
    """Create a 60-card game. IDs are opaque and all hidden zones stay private."""
    rng = _rng(rng)
    players = [str(p) for p in players]
    _require(len(players) == 2 and len(set(players)) == 2, "Exactly two distinct players are required.")
    state = {"rules_version": RULES_VERSION, "players": players, "version": 0,
             "phase": "choose_start", "turn_number": 0, "turn_player": None,
             "first_player": None, "coin_winner": players[rng.randrange(2)],
             "winner": None, "reason": None, "board": {}, "stadium": None,
             "pending_choice": None, "log": [], "tiebreaker": False,
             "original_decks": deepcopy(decks), "resolution": None,
             "setup_bonus_done": False, "setup_stage": "initial", "choice_serial": 0}
    for p in players:
        cards = decks.get(p, [])
        _require(len(cards) == 60, "A TCG deck must contain exactly 60 cards.")
        _require(any(_basic(c) for c in cards), "A deck needs at least one Basic Pokémon.")
        for card in cards:
            _validate_card(card)
        # Use the same name/printing limits and explicit printed exceptions as
        # the deck workshop. The engine must not reject a certified legal deck.
        counts = Counter(c["id"] for c in cards)
        errors = validate_deck([{"card_id": cid, "quantity": n} for cid, n in counts.items()],
                               {c["id"]: c for c in cards})
        _require(not errors, "; ".join(errors))
        state["board"][p] = {"deck": [_instance(c, rng) for c in cards], "hand": [],
                             "prizes": [], "discard": [], "active": None, "bench": [],
                             "lost_zone": [], "setup_ready": False, "mulligans": 0,
                             "mulligan_bonus": 0, "attached_turn": -1,
                             "supporter_turn": -1, "retreated_turn": -1, "stadium_played_turn": -1,
                             "turns_started": 0, "prizes_owed": 0}
        rng.shuffle(state["board"][p]["deck"])
    _log(state, f"{state['coin_winner']} won the opening coin flip and chooses who plays first.")
    return state


def _deal_setup(state: dict, rng) -> None:
    for p in state["players"]:
        b = state["board"][p]
        # Rejected hands are revealed, as required by physical mulligan rules.
        for _ in range(10000):
            _draw(state, p, 7)
            if any(_basic(c["card"]) for c in b["hand"]):
                break
            b["mulligans"] += 1
            _log(state, f"{p} mulligan {b['mulligans']}: " + ", ".join(c["card"]["name"] for c in b["hand"]) + ".")
            b["deck"].extend(b["hand"])
            b["hand"] = []
            rng.shuffle(b["deck"])
        else:
            raise RuleError("Unable to produce an opening hand; retry the game.")
    for p in state["players"]:
        b = state["board"][p]
        b["mulligan_bonus"] = max(0, state["board"][_other(state, p)]["mulligans"] - b["mulligans"])
    state["phase"] = "setup"


def _choice(state: dict, player: str, kind: str, options: list[dict], minimum: int,
            maximum: int, message: str, **extra) -> None:
    state["choice_serial"] += 1
    state["pending_choice"] = {"id": str(state["choice_serial"]), "player": player,
                               "type": kind, "options": options, "min": minimum,
                               "max": maximum, "message": message, **extra}


def _finish_setup(state: dict) -> None:
    if not all(state["board"][p]["setup_ready"] for p in state["players"]):
        return
    if state["setup_stage"] == "initial":
        # Prizes are put down before compensation cards are drawn.
        for p in state["players"]:
            b = state["board"][p]
            b["prizes"] = [b["deck"].pop() for _ in range(6)]
        state["setup_stage"] = "bonus"
        _next_bonus(state)
    else:
        state["phase"] = "playing"
        state["pending_choice"] = None
        _log(state, "Both players reveal their starting Pokémon.")
        _begin_turn(state, state["first_player"])


def _next_bonus(state: dict) -> None:
    for p in state["players"]:
        b = state["board"][p]
        if b["mulligan_bonus"]:
            maximum = min(b["mulligan_bonus"], len(b["deck"]))
            _choice(state, p, "mulligan_bonus", [{"value": n, "label": f"Draw {n} bonus cards"} for n in range(maximum + 1)], 1, 1,
                    "Choose how many compensation cards to draw.")
            return
    state["setup_bonus_done"] = True
    state["setup_stage"] = "reveal"
    for p in state["players"]:
        state["board"][p]["setup_ready"] = False
    state["pending_choice"] = None


def _begin_turn(state: dict, player: str) -> None:
    state["turn_player"] = player
    state["turn_number"] += 1
    b = state["board"][player]
    if not b["deck"]:
        _win(state, _other(state, player), "Opponent could not draw a card to start their turn.")
        return
    b["turns_started"] += 1
    _draw(state, player, 1)
    _log(state, f"{player} begins turn {state['turn_number']} and draws a card.")


def _win(state: dict, player: str, reason: str) -> None:
    state.update(phase="finished", winner=player, reason=reason, pending_choice=None, resolution=None)
    _log(state, f"{player} wins. {reason}")


def _clear_conditions(pokemon: dict) -> None:
    pokemon["statuses"] = []


def _switch(board: dict, target_uid: str) -> None:
    target = _find(board["bench"], target_uid)
    board["bench"].remove(target)
    if board["active"]:
        _clear_conditions(board["active"])
        board["bench"].append(board["active"])
    board["active"] = target


def _energy_units(instance: dict) -> list[str]:
    effect = instance["card"].get("effect", {})
    if effect.get("kind") == "double_colorless":
        return ["Colorless", "Colorless"]
    if effect.get("kind") == "basic_energy":
        return [effect.get("type") or instance["card"].get("types", ["Colorless"])[0]]
    return []


def _has_energy(pokemon: dict, cost: list[str], attachments=None) -> bool:
    available = Counter(t for card in (attachments if attachments is not None else pokemon["attachments"])
                        for t in _energy_units(card))
    for energy_type in cost:
        if energy_type in {"Colorless", "Free"}:
            continue
        if not available[energy_type]:
            return False
        available[energy_type] -= 1
    return sum(available.values()) >= sum(t == "Colorless" for t in cost)


def _retreat_payment(pokemon: dict, cost: int, selected: list[str]) -> bool:
    if len(selected) != len(set(selected)):
        return False
    try:
        energies = [_find(pokemon["attachments"], uid) for uid in selected]
    except RuleError:
        return False
    units = [len(_energy_units(e)) for e in energies]
    # Select Energy units, then discard their cards. One unit from each of two
    # Double Colorless cards can pay a cost of two, discarding both cards.
    return len(units) <= cost <= sum(units) and all(units) if cost else not units


def _energy_selections(energies: list[dict], count: int):
    """Equivalent copies share one UI action, avoiding combinatorial payloads."""
    groups: dict[tuple, list[dict]] = {}
    for energy in energies:
        key = tuple(_energy_units(energy))
        groups.setdefault(key, []).append(energy)
    grouped = list(groups.values())
    for counts in product(*(range(min(count, len(group)) + 1) for group in grouped)):
        if sum(counts) > count:
            continue
        selection = [card for group, number in zip(grouped, counts) for card in group[:number]]
        if sum(len(_energy_units(e)) for e in selection) >= count:
            yield selection


def _damage_after_modifiers(damage: int, attacker: dict, defender: dict) -> int:
    if damage <= 0:
        return 0
    damage += sum(e.get("damage_bonus", 0) for e in _tool_modifiers(attacker))
    types = attacker["card"].get("types", [])
    for weakness in defender["card"].get("weaknesses", []):
        if weakness.get("type") in types:
            value = str(weakness.get("value", ""))
            if value.startswith(("×", "x", "*")):
                damage *= int(value[1:])
            elif value.startswith("+"):
                damage += int(value[1:])
            else:
                raise RuleError("Unsupported printed Weakness.")
            break
    for resistance in defender["card"].get("resistances", []):
        if resistance.get("type") in types:
            value = str(resistance.get("value", ""))
            _require(bool(re.fullmatch(r"-\d+", value)), "Unsupported printed Resistance.")
            damage += int(value)
            break
    return max(0, damage)


def _set_status(pokemon: dict, status: str) -> None:
    _require(status in {"asleep", "confused", "paralyzed", "poisoned", "burned"}, "Unknown Special Condition.")
    if status in {"asleep", "confused", "paralyzed"}:
        pokemon["statuses"] = [s for s in pokemon["statuses"] if s not in {"asleep", "confused", "paralyzed"}]
    if status not in pokemon["statuses"]:
        pokemon["statuses"].append(status)


def _discard_pokemon(board: dict, pokemon: dict) -> None:
    board["discard"].extend(pokemon["evolution"])
    board["discard"].extend(pokemon["attachments"])
    board["discard"].append({"uid": pokemon["uid"], "card": pokemon["card"]})
    if board["active"] is pokemon:
        board["active"] = None
    else:
        board["bench"].remove(pokemon)


def _resolve_knockouts(state: dict, after: str, rng, chooser: str | None = None) -> None:
    order = chooser or state["turn_player"]
    for p in state["players"]:
        b = state["board"][p]
        for pokemon in [b["active"], *list(b["bench"])]:
            if pokemon and pokemon["damage"] >= _effective_hp(pokemon):
                _log(state, f"{pokemon['card']['name']} was Knocked Out.")
                state["board"][_other(state, p)]["prizes_owed"] += int(pokemon["card"].get("prize_count", 1))
                _discard_pokemon(b, pokemon)
    state["resolution"] = {"after": after, "order": [order, _other(state, order)]}
    _continue_resolution(state, rng)


def _check_win_conditions(state: dict, rng) -> bool:
    scores = {}
    for p in state["players"]:
        own, opponent = state["board"][p], state["board"][_other(state, p)]
        scores[p] = int(not own["prizes"]) + int(not opponent["active"] and not opponent["bench"])
    if any(scores.values()):
        high = max(scores.values())
        winners = [p for p in state["players"] if scores[p] == high]
        if len(winners) == 1:
            _win(state, winners[0], "Prize cards and Pokémon remaining determined the result.")
        else:
            _restart_tiebreaker(state, rng)
        return True
    if state["tiebreaker"]:
        a, b = state["players"]
        if len(state["board"][a]["prizes"]) != len(state["board"][b]["prizes"]):
            _win(state, min(state["players"], key=lambda p: len(state["board"][p]["prizes"])),
                 "First Prize-card lead in the tiebreaker game.")
            return True
    return False


def _restart_tiebreaker(state: dict, rng) -> None:
    previous_log, version = state["log"], state["version"]
    next_game = new_game(state["original_decks"], state["players"], rng)
    next_game.update(tiebreaker=True, version=version)
    next_game["log"] = previous_log + [{"turn": 0, "message": "Simultaneous equal win conditions: start a six-Prize tiebreaker game."}] + next_game["log"]
    state.clear()
    state.update(next_game)


def _continue_resolution(state: dict, rng) -> None:
    resolution = state["resolution"]
    for p in resolution["order"]:
        b = state["board"][p]
        owed = min(b["prizes_owed"], len(b["prizes"]))
        if owed:
            _choice(state, p, "prizes", [{"value": i, "label": f"Prize {i + 1}"} for i in range(len(b["prizes"]))], owed, owed,
                    f"Choose {owed} face-down Prize card{'s' if owed != 1 else ''}.")
            return
        b["prizes_owed"] = 0
    if _check_win_conditions(state, rng):
        return
    # Promotion has a different order from on-KO triggered effects: when both
    # Active Pokémon fall, the player whose turn comes next promotes first.
    next_player = _other(state, state["turn_player"])
    for p in [next_player, state["turn_player"]]:
        b = state["board"][p]
        if b["active"] is None and b["bench"]:
            _choice(state, p, "promote", [{"value": c["uid"], "label": c["card"]["name"], "card": c["card"]} for c in b["bench"]], 1, 1,
                    "Choose a new Active Pokémon.")
            return
    after = resolution["after"]
    state["resolution"] = None
    state["pending_choice"] = None
    if after == "end_turn":
        _checkup(state, rng)
    elif after == "begin_turn":
        _begin_turn(state, _other(state, state["turn_player"]))


def _checkup(state: dict, rng) -> None:
    ending = state["turn_player"]
    _log(state, "Pokémon Checkup.")
    for p in state["players"]:
        pokemon = state["board"][p]["active"]
        if pokemon is None:
            continue
        statuses = pokemon["statuses"]
        if "poisoned" in statuses:
            pokemon["damage"] += 10
        if "burned" in statuses:
            pokemon["damage"] += 20
            if _flip(state, rng, f"{pokemon['card']['name']} Burn recovery"):
                statuses.remove("burned")
        if "asleep" in statuses and _flip(state, rng, f"{pokemon['card']['name']} Sleep recovery"):
            statuses.remove("asleep")
        if "paralyzed" in statuses and p == ending:
            statuses.remove("paralyzed")
    _resolve_knockouts(state, "begin_turn", rng, chooser=_other(state, ending))


def _attack(state: dict, player: str, action: dict, rng) -> None:
    b = state["board"][player]
    attacker = b["active"]
    defender = state["board"][_other(state, player)]["active"]
    _require(attacker and defender, "Both players need an Active Pokémon.")
    _require(state["turn_number"] != 1, "The starting player cannot attack on their first turn.")
    _require(not {"asleep", "paralyzed"} & set(attacker["statuses"]), "This Pokémon cannot attack under its Special Condition.")
    index = action.get("attack_index")
    attacks = attacker["card"].get("attacks", [])
    _require(type(index) is int and 0 <= index < len(attacks), "Select a printed attack.")
    attack = attacks[index]
    _require(_has_energy(attacker, attack.get("cost", [])), "The Active Pokémon lacks the required attached Energy.")
    effects = _effects(attack.get("effect"))
    _require(all(e.get("kind") in ATTACK_EFFECTS for e in effects), "Unsupported attack effect.")
    # Costs chosen before random outcomes; illegal commands never get a free coin flip.
    discard_effects = [e for e in effects if e["kind"] == "discard_self_energy"]
    if discard_effects:
        _require(len(discard_effects) == 1, "Multiple Energy-discard selections are not implemented.")
        e = discard_effects[0]
        selected = action.get("energy_uids", [])
        allowed = [c for c in attacker["attachments"] if not e.get("type") or e["type"] in _energy_units(c)]
        needed = min(int(e["count"]), sum(len(_energy_units(c)) for c in allowed))
        _require(all(uid in {c["uid"] for c in allowed} for uid in selected) and _retreat_payment(attacker, needed, selected),
                 f"Select attached Energy cards supplying {needed} Energy for the attack effect.")
    _log(state, f"{attacker['card']['name']} uses {attack['name']}.")
    if "confused" in attacker["statuses"] and not _flip(state, rng, "Confusion"):
        attacker["damage"] += 30
        _resolve_knockouts(state, "end_turn", rng)
        return
    raw_damage = str(attack.get("damage", "") or "")
    damage = int(re.match(r"\d+", raw_damage).group()) if re.match(r"\d+", raw_damage) else 0
    failed = False
    for effect in effects:
        kind = effect["kind"]
        if kind == "flip_fail" and not _flip(state, rng, attack["name"]):
            failed = True
            break
        if kind == "flip_damage":
            damage = sum(_flip(state, rng, attack["name"]) for _ in range(int(effect["count"]))) * int(effect["amount"])
        elif kind == "flip_bonus" and _flip(state, rng, attack["name"]):
            damage += int(effect["amount"])
    if not failed:
        final_damage = _damage_after_modifiers(damage, attacker, defender)
        defender["damage"] += final_damage
        _log(state, f"{defender['card']['name']} takes {final_damage} damage.")
        for effect in effects:
            kind = effect["kind"]
            if kind == "condition":
                _set_status(defender, effect["status"])
            elif kind == "flip_condition" and _flip(state, rng, attack["name"]):
                _set_status(defender, effect["status"])
            elif kind == "draw":
                _draw(state, player, int(effect["count"]))
            elif kind == "heal_self":
                attacker["damage"] = max(0, attacker["damage"] - int(effect["amount"]))
            elif kind == "recoil":
                attacker["damage"] += int(effect["amount"])
            elif kind == "discard_self_energy":
                for uid in action.get("energy_uids", []):
                    b["discard"].append(_pop(attacker["attachments"], uid))
    _resolve_knockouts(state, "end_turn", rng)


def _trainer(state: dict, player: str, action: dict, rng) -> None:
    b = state["board"][player]
    instance = _find(b["hand"], action.get("card_uid"))
    card = instance["card"]
    _require(card["supertype"] == "Trainer", "Select a Trainer from your hand.")
    _validate_card(card)
    effect = card.get("effect", {})
    kind = effect.get("kind")
    _require(kind in TRAINER_EFFECTS, "This Trainer effect is not implemented.")
    if "Supporter" in card.get("subtypes", []):
        _require(state["turn_number"] != 1, "The starting player cannot play a Supporter on their first turn.")
        _require(b["supporter_turn"] != state["turn_number"], "You have already played a Supporter this turn.")
    target = None
    if kind == "tool":
        target = _pokemon(b, action.get("target_uid"))
        _require(not _tools(target), "This Pokémon already has a Pokémon Tool attached.")
    elif kind == "stadium":
        _require(b.get("stadium_played_turn", -1) != state["turn_number"], "You already played a Stadium this turn.")
        current = state.get("stadium")
        _require(not current or current["card"]["name"] != card["name"], "A Stadium with that name is already in play.")
    elif kind == "heal":
        target = _pokemon(b, action.get("target_uid"))
        _require(target["damage"] > 0, "That Pokémon has no damage to heal.")
    elif kind in {"switch", "gust"}:
        board = b if kind == "switch" else state["board"][_other(state, player)]
        _require(any(c["uid"] == action.get("target_uid") for c in board["bench"]), "Select a Benched Pokémon.")
    elif kind in {"draw", "discard_hand_draw", "shuffle_hand_draw", "search_basic", "search_energy"}:
        _require(bool(b["deck"]) or (kind == "shuffle_hand_draw" and len(b["hand"]) > 1), "This Trainer would have no effect.")
    b["hand"].remove(instance)
    # The played Trainer is neither in hand nor in discard while its effect resolves.
    if "Supporter" in card.get("subtypes", []):
        b["supporter_turn"] = state["turn_number"]
    _log(state, f"{player} plays {card['name']}.")
    if kind == "tool":
        target["attachments"].append(instance)
        _resolve_knockouts(state, "resume", rng)
        return
    if kind == "stadium":
        previous = state.get("stadium")
        if previous:
            state["board"][previous["owner"]]["discard"].append({"uid": previous["uid"], "card": previous["card"]})
        state["stadium"] = {**instance, "owner": player, "used_turns": {}}
        b["stadium_played_turn"] = state["turn_number"]
        _resolve_knockouts(state, "resume", rng)
        return
    if kind in {"draw", "discard_hand_draw", "shuffle_hand_draw"}:
        if kind == "discard_hand_draw":
            b["discard"].extend(b["hand"])
            b["hand"] = []
        elif kind == "shuffle_hand_draw" and b["hand"]:
            b["deck"].extend(b["hand"])
            b["hand"] = []
            rng.shuffle(b["deck"])
        _draw(state, player, int(effect["count"]))
    elif kind == "heal":
        target["damage"] = max(0, target["damage"] - int(effect["amount"]))
    elif kind in {"switch", "gust"}:
        _switch(b if kind == "switch" else state["board"][_other(state, player)], action["target_uid"])
    elif kind in {"search_basic", "search_energy"}:
        predicate = _basic if kind == "search_basic" else _basic_energy
        options = [{"value": c["uid"], "label": c["card"]["name"], "card": c["card"]}
                   for c in b["deck"] if predicate(c["card"])]
        _choice(state, player, "search", options, 0, min(int(effect.get("count", 1)), len(options)),
                "Search your deck. You may fail a search for a specified card type; selected cards are revealed.",
                trainer=instance)
        return
    b["discard"].append(instance)


def _ability_base(state: dict, player: str, source: dict, index: int) -> tuple[dict, dict]:
    abilities = source["card"].get("abilities", [])
    _require(type(index) is int and 0 <= index < len(abilities), "Select a printed Ability.")
    ability = abilities[index]
    _validate_ability(ability)
    effect = ability["effect"]
    if effect.get("active_only"):
        _require(state["board"][player]["active"] is source, "This Ability can only be used from the Active Spot.")
    key = ability.get("name", str(index))
    _require(not effect["once_per_turn"] or source.get("abilities_used", {}).get(key) != state["turn_number"],
             "This Pokémon already used that Ability this turn.")
    return ability, effect


def _ability_targets(board: dict, effect: dict) -> list[dict]:
    pokemon = list(board["bench"])
    if not effect.get("benched_only") and board["active"]:
        pokemon.insert(0, board["active"])
    return [p for p in pokemon if not effect.get("target_type") or effect["target_type"] in p["card"].get("types", [])]


def _ability(state: dict, player: str, action: dict, rng) -> None:
    b = state["board"][player]
    source = _pokemon(b, action.get("pokemon_uid"))
    ability, effect = _ability_base(state, player, source, action.get("ability_index"))
    kind = effect["kind"]
    if kind in {"draw", "draw_to"}:
        count = effect["count"] if kind == "draw" else max(0, effect["hand_size"] - len(b["hand"]))
        _require(count > 0 and b["deck"], "This Ability would draw no cards.")
        _draw(state, player, count)
    elif kind == "heal_self":
        _require(source["damage"] > 0, "This Pokémon has no damage to heal.")
        source["damage"] = max(0, source["damage"] - effect["amount"])
    elif kind == "attach_energy":
        card = _find(b["hand"], action.get("card_uid"))
        _require(_basic_energy(card["card"]) and effect["energy_type"] in _energy_units(card),
                 "Select a Basic Energy of the Ability's specified type from your hand.")
        target = _pokemon(b, action.get("target_uid"))
        _require(target in _ability_targets(b, effect), "That Pokémon is not an eligible Ability target.")
        target["attachments"].append(_pop(b["hand"], card["uid"]))
        _log(state, f"{player} attaches {card['card']['name']} to {target['card']['name']} using {ability['name']}.")
    source.setdefault("abilities_used", {})[ability.get("name", str(action["ability_index"]))] = state["turn_number"]
    source["ability_used_turn"] = state["turn_number"]
    _log(state, f"{source['card']['name']} uses {ability['name']}.")
    _resolve_knockouts(state, "resume", rng)


def _stadium_heal_targets(state: dict, player: str) -> list[dict]:
    stadium = state.get("stadium")
    if not stadium:
        return []
    effect = stadium["card"].get("effect", {})
    _validate_modifier(effect)
    if not effect.get("heal") or stadium.get("used_turns", {}).get(player) == state["turn_number"]:
        return []
    b = state["board"][player]
    return [p for p in [b["active"], *b["bench"]] if p and p["damage"] > 0
            and _matches(p, effect.get("applies_to"))
            and (not effect.get("heal_types") or set(p["card"].get("types", [])) & set(effect["heal_types"]))]


def _use_stadium(state: dict, player: str, action: dict, rng) -> None:
    stadium = state.get("stadium")
    _require(stadium, "There is no Stadium in play.")
    targets = _stadium_heal_targets(state, player)
    _require(targets, "This Stadium cannot be used again or has no eligible damaged Pokémon.")
    effect = stadium["card"]["effect"]
    if not effect.get("heal_all"):
        targets = [p for p in targets if p["uid"] == action.get("target_uid")]
        _require(targets, "Choose an eligible damaged Pokémon.")
    for target in targets:
        target["damage"] = max(0, target["damage"] - effect["heal"])
    stadium.setdefault("used_turns", {})[player] = state["turn_number"]
    _log(state, f"{player} uses {stadium['card']['name']} to heal {len(targets)} Pokémon.")
    _resolve_knockouts(state, "resume", rng)


def _choose(state: dict, player: str, action: dict, rng) -> None:
    choice = state["pending_choice"]
    _require(choice and choice["player"] == player, "There is no choice waiting for you.")
    _require(str(action.get("choice_id")) == choice["id"], "This choice is stale.")
    selected = action.get("selected", action.get("selected_uids", []))
    _require(isinstance(selected, list), "Choose a list of options.")
    _require(len(selected) == len(set(map(str, selected))), "Select each option at most once.")
    _require(choice["min"] <= len(selected) <= choice["max"], "Wrong number of selected options.")
    options = {str(o["value"]): o["value"] for o in choice["options"]}
    _require(all(str(value) in options for value in selected), "Invalid choice option.")
    selected = [options[str(value)] for value in selected]
    b = state["board"][player]
    kind = choice["type"]
    state["pending_choice"] = None
    if kind == "mulligan_bonus":
        _draw(state, player, int(selected[0]))
        _log(state, f"{player} draws {selected[0]} mulligan compensation cards.")
        b["mulligan_bonus"] = 0
        _next_bonus(state)
    elif kind == "search":
        for uid in selected:
            card = _pop(b["deck"], uid)
            b["hand"].append(card)
            _log(state, f"{player} reveals {card['card']['name']} from the deck search.")
        rng.shuffle(b["deck"])
        b["discard"].append(choice["trainer"])
    elif kind == "prizes":
        for index in sorted(selected, reverse=True):
            b["hand"].append(b["prizes"].pop(index))
        b["prizes_owed"] = 0
        _log(state, f"{player} takes {len(selected)} Prize card(s).")
        _continue_resolution(state, rng)
    elif kind == "promote":
        _switch(b, selected[0])
        _log(state, f"{player} promotes {b['active']['card']['name']}.")
        _continue_resolution(state, rng)
    else:
        raise RuleError("This forced choice is not implemented.")


def apply_action(state: dict, user_id: str, action: dict, rng=None) -> dict:
    """Apply one validated command to a copy. Database versioning handles concurrency."""
    user_id = str(user_id)
    _require(user_id in state["players"], "Only match participants may act.")
    _require(state["phase"] != "finished", "This game is finished.")
    _require(isinstance(action, dict), "An action object is required.")
    if "version" in action:
        _require(action["version"] == state["version"], "This game state is stale.")
    result = deepcopy(state)
    rng = _rng(rng)
    _apply(result, user_id, action, rng)
    result["version"] = state["version"] + 1
    return result


def _apply(state: dict, player: str, action: dict, rng) -> None:
    kind = action.get("type")
    b = state["board"][player]
    if kind == "surrender":
        _win(state, _other(state, player), "Opponent surrendered.")
        return
    if state["pending_choice"]:
        _require(kind == "choose", "Resolve the pending choice before another action.")
        _choose(state, player, action, rng)
        return
    if state["phase"] == "choose_start":
        _require(kind == "choose_start" and player == state["coin_winner"], "The coin-flip winner chooses who starts.")
        _require(action.get("first_player") in state["players"], "Choose a match participant.")
        state["first_player"] = action["first_player"]
        _log(state, f"{state['first_player']} will play first.")
        _deal_setup(state, rng)
        return
    if state["phase"] == "setup":
        _require(not b["setup_ready"], "You are already ready for this setup step.")
        if kind == "setup":
            _require(b["active"] is None, "Your starting Active Pokémon is already selected.")
            card = _find(b["hand"], action.get("active_uid"))
            _require(_basic(card["card"]), "Your starting Active must be a Basic Pokémon.")
            b["active"] = _in_play(_pop(b["hand"], card["uid"]), 0)
            for uid in action.get("bench_uids", []):
                _bench(state, player, uid)
        elif kind == "bench":
            _bench(state, player, action.get("card_uid"))
        elif kind == "setup_ready":
            _require(b["active"] is not None, "Choose an Active Pokémon first.")
            b["setup_ready"] = True
            _finish_setup(state)
        else:
            raise RuleError("Choose your starting Pokémon and confirm setup.")
        return
    _require(state["phase"] == "playing" and state["turn_player"] == player, "It is not your turn.")
    if kind == "bench":
        _bench(state, player, action.get("card_uid"))
    elif kind == "attach":
        _require(b["attached_turn"] != state["turn_number"], "You already attached an Energy from hand this turn.")
        card = _find(b["hand"], action.get("card_uid"))
        _require(card["card"]["supertype"] == "Energy", "Select an Energy card in your hand.")
        target = _pokemon(b, action.get("target_uid"))
        target["attachments"].append(_pop(b["hand"], card["uid"]))
        b["attached_turn"] = state["turn_number"]
        _log(state, f"{player} attaches {card['card']['name']} to {target['card']['name']}.")
    elif kind == "evolve":
        card = _find(b["hand"], action.get("card_uid"))
        target = _pokemon(b, action.get("target_uid"))
        _require(card["card"]["supertype"] == "Pokémon" and card["card"].get("evolves_from") == target["card"]["name"], "That Pokémon does not evolve from this Pokémon.")
        _require(b["turns_started"] > 1, "You cannot evolve during your first turn.")
        _require(target["entered_turn"] < state["turn_number"] and target["evolved_turn"] != state["turn_number"], "This Pokémon cannot evolve again yet.")
        instance = _pop(b["hand"], card["uid"])
        target["evolution"].append({"uid": target["uid"], "card": target["card"]})
        target.update(instance)
        target["evolved_turn"] = state["turn_number"]
        _clear_conditions(target)
        _log(state, f"{player} evolves into {target['card']['name']}.")
        _resolve_knockouts(state, "continue", rng)
    elif kind == "play_trainer":
        _trainer(state, player, action, rng)
    elif kind == "ability":
        _ability(state, player, action, rng)
    elif kind == "use_stadium":
        _use_stadium(state, player, action, rng)
    elif kind == "retreat":
        active = b["active"]
        _require(active and b["bench"], "You need an Active and a Benched Pokémon to retreat.")
        _require(b["retreated_turn"] != state["turn_number"], "You already retreated this turn.")
        _require(not {"asleep", "paralyzed"} & set(active["statuses"]), "This Special Condition prevents retreat.")
        _require(any(c["uid"] == action.get("target_uid") for c in b["bench"]), "Choose a Benched Pokémon.")
        selected = action.get("energy_uids", [])
        _require(_retreat_payment(active, _retreat_cost(state, active), selected), "Select enough attached Energy for the printed retreat cost.")
        for uid in selected:
            b["discard"].append(_pop(active["attachments"], uid))
        _switch(b, action["target_uid"])
        b["retreated_turn"] = state["turn_number"]
        _log(state, f"{player} retreats and promotes {b['active']['card']['name']}.")
    elif kind == "attack":
        _attack(state, player, action, rng)
    elif kind == "end_turn":
        _checkup(state, rng)
    else:
        raise RuleError("Unknown or unsupported action.")


def _bench(state: dict, player: str, uid: str) -> None:
    b = state["board"][player]
    _require(len(b["bench"]) < 5, "Your Bench is full.")
    card = _find(b["hand"], uid)
    _require(_basic(card["card"]), "Only a Basic Pokémon can be played directly to the Bench.")
    b["bench"].append(_in_play(_pop(b["hand"], uid), state["turn_number"]))
    if state["phase"] == "playing":
        _log(state, f"{player} plays {card['card']['name']} to the Bench.")


def _legal_actions(state: dict, player: str) -> list[dict]:
    """Executable actions for a simple client. Server still revalidates every action."""
    if state["phase"] == "finished":
        return []
    actions = []
    def add(label, **action):
        actions.append({"label": label, "action": action})
    add("Surrender", type="surrender")
    if state["pending_choice"]:
        return actions
    b = state["board"][player]
    if state["phase"] == "choose_start":
        if player == state["coin_winner"]:
            for p in state["players"]:
                add("Play first" if p == player else "Play second", type="choose_start", first_player=p)
        return actions
    if state["phase"] == "setup":
        if not b["setup_ready"]:
            for c in b["hand"]:
                if _basic(c["card"]):
                    if b["active"] is None:
                        add(f"Active: {c['card']['name']}", type="setup", active_uid=c["uid"])
                    elif len(b["bench"]) < 5:
                        add(f"Bench: {c['card']['name']}", type="bench", card_uid=c["uid"])
            if b["active"]:
                add("Confirm starting Pokémon" if state["setup_stage"] == "initial" else "Reveal and begin", type="setup_ready")
        return actions
    if state["turn_player"] != player:
        return actions
    pokemon = [p for p in [b["active"], *b["bench"]] if p]
    for c in b["hand"]:
        card, uid = c["card"], c["uid"]
        if _basic(card) and len(b["bench"]) < 5:
            add(f"Bench {card['name']}", type="bench", card_uid=uid)
        if card["supertype"] == "Energy" and b["attached_turn"] != state["turn_number"]:
            for target in pokemon:
                add(f"Attach {card['name']} → {target['card']['name']}", type="attach", card_uid=uid, target_uid=target["uid"])
        if card.get("evolves_from") and b["turns_started"] > 1:
            for target in pokemon:
                if target["card"]["name"] == card["evolves_from"] and target["entered_turn"] < state["turn_number"] and target["evolved_turn"] != state["turn_number"]:
                    add(f"Evolve {target['card']['name']} → {card['name']}", type="evolve", card_uid=uid, target_uid=target["uid"])
        if card["supertype"] == "Trainer":
            if "Supporter" in card.get("subtypes", []) and (state["turn_number"] == 1 or b["supporter_turn"] == state["turn_number"]):
                continue
            kind = card.get("effect", {}).get("kind")
            targets = None
            if kind == "stadium":
                current = state.get("stadium")
                if b.get("stadium_played_turn", -1) != state["turn_number"] and (not current or current["card"]["name"] != card["name"]):
                    add(f"Play Stadium: {card['name']}", type="play_trainer", card_uid=uid)
                continue
            if kind == "tool":
                targets = [p for p in pokemon if not _tools(p)]
            elif kind == "heal":
                targets = [p for p in pokemon if p["damage"] > 0]
            elif kind == "switch":
                targets = b["bench"]
            elif kind == "gust":
                targets = state["board"][_other(state, player)]["bench"]
            if targets is not None:
                for target in targets:
                    add(f"{card['name']} → {target['card']['name']}", type="play_trainer", card_uid=uid, target_uid=target["uid"])
            elif kind in TRAINER_EFFECTS and (b["deck"] or (kind == "shuffle_hand_draw" and len(b["hand"]) > 1)):
                add(f"Play {card['name']}", type="play_trainer", card_uid=uid)
    for source in pokemon:
        for index, ability in enumerate(source["card"].get("abilities", [])):
            try:
                _, effect = _ability_base(state, player, source, index)
            except RuleError:
                continue
            kind = effect["kind"]
            command = {"type": "ability", "pokemon_uid": source["uid"], "ability_index": index}
            label = f"{source['card']['name']}: {ability['name']}"
            if kind == "attach_energy":
                for card in b["hand"]:
                    if _basic_energy(card["card"]) and effect["energy_type"] in _energy_units(card):
                        for target in _ability_targets(b, effect):
                            add(f"{label} → {target['card']['name']} ({card['card']['name']})", **command, card_uid=card["uid"], target_uid=target["uid"])
            elif kind == "draw" and b["deck"]:
                add(label, **command)
            elif kind == "draw_to" and b["deck"] and len(b["hand"]) < effect["hand_size"]:
                add(label, **command)
            elif kind == "heal_self" and source["damage"] > 0:
                add(label, **command)
    stadium_targets = _stadium_heal_targets(state, player)
    if stadium_targets:
        if state["stadium"]["card"]["effect"].get("heal_all"):
            add(f"Use {state['stadium']['card']['name']}", type="use_stadium")
        else:
            for target in stadium_targets:
                add(f"{state['stadium']['card']['name']} → {target['card']['name']}", type="use_stadium", target_uid=target["uid"])
    active = b["active"]
    if active:
        if state["turn_number"] != 1 and not {"asleep", "paralyzed"} & set(active["statuses"]):
            for index, attack in enumerate(active["card"].get("attacks", [])):
                if not _has_energy(active, attack.get("cost", [])):
                    continue
                discards = [e for e in _effects(attack.get("effect")) if e["kind"] == "discard_self_energy"]
                if discards:
                    e = discards[0]
                    energies = [c for c in active["attachments"] if not e.get("type") or e["type"] in _energy_units(c)]
                    count = min(int(e["count"]), sum(len(_energy_units(c)) for c in energies))
                    for selection in _energy_selections(energies, count):
                        label = f"Attack: {attack['name']} (discard " + ", ".join(c["card"]["name"] for c in selection) + ")"
                        add(label, type="attack", attack_index=index, energy_uids=[c["uid"] for c in selection])
                else:
                    add(f"Attack: {attack['name']}", type="attack", attack_index=index)
        if b["retreated_turn"] != state["turn_number"] and not {"asleep", "paralyzed"} & set(active["statuses"]):
            cost = _retreat_cost(state, active)
            energy = [c for c in active["attachments"] if _energy_units(c)]
            for selection in _energy_selections(energy, cost):
                uids = [c["uid"] for c in selection]
                for target in b["bench"]:
                    payment = ", ".join(c["card"]["name"] for c in selection) or "free"
                    add(f"Retreat → {target['card']['name']} ({payment})", type="retreat", target_uid=target["uid"], energy_uids=uids)
    add("End turn", type="end_turn")
    return actions


def _project_pokemon(pokemon: dict | None, state: dict) -> dict | None:
    if pokemon is None:
        return None
    result = deepcopy(pokemon)
    result["effective_hp"] = _effective_hp(pokemon)
    result["remaining_hp"] = max(0, result["effective_hp"] - pokemon["damage"])
    result["effective_retreat_cost"] = _retreat_cost(state, pokemon)
    return result


def project_state(state: dict, user_id: str) -> dict:
    """Construct an allowlisted projection; hidden identifiers never cross the wire."""
    user_id = str(user_id)
    _require(user_id in state["players"], "Only match participants may view this game.")
    result = {key: deepcopy(state[key]) for key in (
        "rules_version", "players", "version", "phase", "turn_number", "turn_player",
        "first_player", "coin_winner", "winner", "reason", "stadium", "tiebreaker", "log")}
    result.update(you=user_id, board={}, pending_choice=None, legal_actions=_legal_actions(state, user_id))
    for p in state["players"]:
        b = state["board"][p]
        hidden_setup = state["phase"] == "setup" and p != user_id
        result["board"][p] = {
            "active": {"face_down": True} if hidden_setup and b["active"] else _project_pokemon(b["active"], state),
            "bench": [{"face_down": True} for _ in b["bench"]] if hidden_setup else [_project_pokemon(p, state) for p in b["bench"]],
            "hand": deepcopy(b["hand"]) if p == user_id else {"count": len(b["hand"])},
            "deck": {"count": len(b["deck"])}, "prizes": {"count": len(b["prizes"])},
            "discard": deepcopy(b["discard"]), "lost_zone": deepcopy(b["lost_zone"]),
            "setup_ready": b["setup_ready"], "mulligans": b["mulligans"],
            "mulligan_bonus": b["mulligan_bonus"], "turns_started": b["turns_started"],
        }
    choice = state["pending_choice"]
    if choice:
        keys = ("id", "player", "type", "message")
        result["pending_choice"] = {key: choice[key] for key in keys}
        if choice["player"] == user_id:
            result["pending_choice"].update(min=choice["min"], max=choice["max"])
            result["pending_choice"]["options"] = deepcopy(choice["options"])
        else:
            result["pending_choice"]["message"] = "Waiting for your opponent to complete a choice."
    return result
