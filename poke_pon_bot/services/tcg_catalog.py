"""Conservative executable TCG catalog and physical deck construction rules.

Upstream text is never treated as code. Every supported attack consumes its entire
text through exact templates. Unknown abilities, conditions, and rule boxes block
the whole printing; no attack or rule is silently discarded. Definitions are
snapshotted by the match service, so later imports cannot change an active game.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re
import unicodedata
from typing import Any

DEFINITION_VERSION = "tcg-2026-09-09-v2"
ERRATA_SOURCE = "https://play.pokemon.com/en-us/resources/documents/tcg-errata/"
ENERGY_TYPES = {"Grass", "Fire", "Water", "Lightning", "Psychic", "Fighting", "Darkness", "Metal", "Fairy"}
POKEMON_TAGS = {"Basic", "Stage 1", "Stage 2", "ex", "EX", "V", "Prime", "Team Plasma", "SP", "Rapid Strike", "Single Strike", "Fusion Strike", "Ancient", "Future", "Ultra Beast"}
TRAINER_BOILERPLATE = {
    "You may play as many Item cards as you like during your turn (before your attack).",
    "You may play any number of Item cards during your turn.",
    "You may play only 1 Supporter card during your turn (before your attack).",
    "You may play only 1 Supporter card during your turn.",
    "You can play only one Supporter card each turn. When you play this card, put it next to your Active Pokémon. When your turn ends, discard this card.",
}
TOOL_BOILERPLATE = {
    "Attach a Pokémon Tool to 1 of your Pokémon that doesn't already have a Pokémon Tool attached to it.",
    "Attach a Pokémon Tool to 1 of your Pokémon that doesn't already have a Pokémon Tool attached.",
    "You may attach any number of Pokémon Tools to your Pokémon during your turn. You may attach only 1 Pokémon Tool to each Pokémon, and it stays attached.",
}
STADIUM_BOILERPLATE = {
    "You may play only 1 Stadium card during your turn. Put it next to the Active Spot, and discard it if another Stadium comes into play. A Stadium with the same name can't be played.",
    "You may play only 1 Stadium card during your turn. Put it into the Active Spot, and discard it if another Stadium comes into play. A Stadium with the same name can't be played.",
    "This card stays in play when you play it. Discard this card if another Stadium card comes into play. If another card with the same name is in play, you can't play this card.",
    "This Stadium stays in play when you play it. Discard it if another Stadium comes into play. If a Stadium with the same name is in play, you can't play this card.",
    "This card stays in play when you play it. Discard this card if another Stadium card comes into play.",
}
PRIZE_RULES = {
    "V rule: When your Pokémon V is Knocked Out, your opponent takes 2 Prize cards.": 2,
    "Pokémon ex rule: When your Pokémon ex is Knocked Out, your opponent takes 2 Prize cards.": 2,
    "Pokémon-EX rule: When a Pokémon-EX has been Knocked Out, your opponent takes 2 Prize cards.": 2,
    "When Pokémon-ex has been Knocked Out, your opponent takes 2 Prize cards.": 2,
}
UNLIMITED_COPIES_RULE = "You may have as many of this card in your deck as you like."
UNOWN_COPIES_RULE = "You may have up to 4 Basic Pokémon cards in your deck with Unown in their names."


def _text(value: Any) -> str:
    return " ".join(str(value or "").replace("’", "'").split())


def _int(value: Any) -> int:
    return int(value) if re.fullmatch(r"\d+", str(value or "")) else 0


def _effect(text: str, name: str) -> dict | None:
    """One complete supported attack text (never a substring match)."""
    if not text:
        return {"kind": "damage"}
    if text == "Flip a coin. If tails, this attack does nothing.":
        return {"kind": "flip_fail"}
    m = re.fullmatch(r"Flip (\d+) coins\. This attack does (\d+) damage (?:times the number of heads|for each heads)\.", text)
    if m:
        return {"kind": "flip_damage", "count": int(m[1]), "amount": int(m[2])}
    m = re.fullmatch(r"Flip a coin\. If heads, this attack does (\d+) (?:damage plus (\d+) more damage|more damage)\.", text)
    if m:
        return {"kind": "flip_bonus", "amount": int(m[2] or m[1])}
    m = re.fullmatch(r"(Flip a coin\. If heads, )?(?:[Tt]he Defending Pokémon|[Yy]our opponent's Active Pokémon) is now (Asleep|Burned|Confused|Paralyzed|Poisoned)\.", text)
    if m:
        return {"kind": "flip_condition" if m[1] else "condition", "status": m[2].lower()}
    m = re.fullmatch(r"Draw (a card|\d+ cards)\.", text)
    if m:
        return {"kind": "draw", "count": 1 if m[1] == "a card" else int(m[1].split()[0])}
    m = re.fullmatch(r"Heal (\d+) damage from this Pokémon\.", text)
    if m:
        return {"kind": "heal_self", "amount": int(m[1])}
    m = re.fullmatch(r"(?:This Pokémon|" + re.escape(name) + r") (?:also )?does (\d+) damage to itself\.", text)
    if m:
        return {"kind": "recoil", "amount": int(m[1])}
    m = re.fullmatch(r"Discard (an|\d+) Energy (?:attached to|from) this Pokémon\.", text)
    if m:
        return {"kind": "discard_self_energy", "count": 1 if m[1] == "an" else int(m[1])}
    m = re.fullmatch(r"Discard (a|an|\d+) (" + "|".join(sorted(ENERGY_TYPES)) + r") Energy (?:attached to|from) this Pokémon\.", text)
    if m:
        return {"kind": "discard_self_energy", "count": 1 if m[1] in ("a", "an") else int(m[1]), "type": m[2]}
    return None


def attack_effect(attack: dict, name: str) -> dict | list[dict] | None:
    text = _text(attack.get("text"))
    effect = _effect(text, name)
    if effect is None:
        # Independent unconditional sentences can resolve in sequence. Never split
        # coin-flip clauses; doing so would change their conditional scope.
        parts = re.split(r"(?<=\.) ", text)
        if len(parts) > 1 and "Flip" not in text and "If " not in text:
            effects = [_effect(part, name) for part in parts]
            if all(effects):
                effect = effects
    damage = str(attack.get("damage") or "")
    kinds = {e["kind"] for e in (effect if isinstance(effect, list) else [effect]) if e}
    if re.fullmatch(r"\d*", damage):
        return effect
    if re.fullmatch(r"\d+[×x]", damage) and "flip_damage" in kinds:
        return effect
    if re.fullmatch(r"\d+\+", damage) and "flip_bonus" in kinds:
        return effect
    # Variable damage without an exact implemented calculation is never numeric.
    return None


def _tool_effect(card: dict) -> dict | None:
    text = " ".join(r for r in card["rules"] if r not in TRAINER_BOILERPLATE | TOOL_BOILERPLATE)
    m = re.fullmatch(r"The (Basic |Stage 1 |Stage 2 )?Pokémon this card is attached to gets \+(\d+) HP(, except Pokémon-GX)?\.", text)
    if m:
        effect = {"kind": "tool", "hp_bonus": int(m[2])}
        scope = {}
        if m[1]:
            scope["subtypes"] = [m[1].strip()]
        if m[3]:
            scope["exclude_subtypes"] = ["GX"]
        if scope:
            effect["applies_to"] = scope
        return effect
    if text == "The Pokémon this card is attached to has no Retreat Cost.":
        return {"kind": "tool", "retreat_free": True}
    m = re.fullmatch(r"The Retreat Cost of the Pokémon this card is attached to is (Colorless(?:Colorless)*) less\.", text)
    if m:
        return {"kind": "tool", "retreat_reduction": m[1].count("Colorless")}
    m = re.fullmatch(r"The attacks of the Pokémon this card is attached to do (\d+) more damage to your opponent's Active Pokémon \(before applying Weakness and Resistance\)\.", text)
    if m:
        return {"kind": "tool", "damage_bonus": int(m[1])}
    return None


def _stadium_effect(card: dict) -> dict | None:
    text = " ".join(r for r in card["rules"] if r not in STADIUM_BOILERPLATE)
    # Skyarrow Bridge's upstream text contains the original transcription "is play".
    m = re.fullmatch(r"The Retreat Cost of each (Basic |Stage 1 |Stage 2 )?Pokémon (?:(?:in|is) play(?: \(both yours and your opponent's\))? )?is (Colorless(?:Colorless)*) less\.", text)
    if m:
        effect = {"kind": "stadium", "retreat_reduction": m[2].count("Colorless")}
        if m[1]:
            effect["applies_to"] = {"subtypes": [m[1].strip()]}
        return effect
    m = re.fullmatch(r"Once during each player's turn, that player may heal (\d+) damage from each of (?:his or her|their) (" + "|".join(sorted(ENERGY_TYPES)) + r") Pokémon and (" + "|".join(sorted(ENERGY_TYPES)) + r") Pokémon\.", text)
    if m:
        return {"kind": "stadium", "heal": int(m[1]), "heal_all": True, "heal_types": [m[2], m[3]]}
    m = re.fullmatch(r"Once during each player's turn, that player may heal (\d+) damage from 1 of (?:his or her|their) Pokémon\.", text)
    if m:
        return {"kind": "stadium", "heal": int(m[1])}
    return None


def _ability_effect(ability: dict) -> dict | None:
    # Poké-Powers/Pokémon Powers carry different Special Condition rules.
    if ability.get("type") != "Ability":
        return None
    text = _text(ability.get("text"))
    prefix = r"Once during your turn(?: \(before your attack\))?, "
    active = r"(if this Pokémon is (?:in the Active Spot|your Active Pokémon), )?"
    m = re.fullmatch(prefix + active + r"you may draw (a card|\d+ cards)\.", text)
    if m:
        return {"kind": "draw", "count": 1 if m[2] == "a card" else int(m[2].split()[0]),
                "once_per_turn": True, **({"active_only": True} if m[1] else {})}
    m = re.fullmatch(prefix + active + r"you may draw cards until you have (\d+) cards in your hand\.", text)
    if m:
        return {"kind": "draw_to", "hand_size": int(m[2]), "once_per_turn": True,
                **({"active_only": True} if m[1] else {})}
    m = re.fullmatch(prefix + active + r"you may heal (\d+) damage from this Pokémon\.", text)
    if m:
        return {"kind": "heal_self", "amount": int(m[2]), "once_per_turn": True,
                **({"active_only": True} if m[1] else {})}
    energy_types = "|".join(sorted(ENERGY_TYPES))
    m = re.fullmatch(r"(As often as you like|Once) during your turn(?: \(before your attack\))?, you may attach a (?:[Bb]asic )?(" + energy_types + r") Energy card from your hand to 1 of your (Benched )?(?:(" + energy_types + r") )?Pokémon\.", text)
    if m:
        return {"kind": "attach_energy", "energy_type": m[2], "basic_only": True,
                "once_per_turn": m[1] == "Once", **({"benched_only": True} if m[3] else {}),
                **({"target_type": m[4]} if m[4] else {})}
    return None


def _trainer_effect(card: dict) -> dict | None:
    if "Pokémon Tool" in card["subtypes"]:
        return _tool_effect(card)
    if "Stadium" in card["subtypes"]:
        return _stadium_effect(card)
    text = " ".join(r for r in card["rules"] if r not in TRAINER_BOILERPLATE)
    if card["name"] == "Potion":
        # Official errata applies to every printing, including 20-damage Potion.
        return {"kind": "heal", "amount": 30}
    m = re.fullmatch(r"Draw (\d+) cards\.", text)
    if m:
        return {"kind": "draw", "count": int(m[1])}
    m = re.fullmatch(r"Discard your hand and draw (\d+) cards\.", text)
    if m:
        return {"kind": "discard_hand_draw", "count": int(m[1])}
    m = re.fullmatch(r"Shuffle your hand into your deck\. Then, draw (\d+) cards\.", text)
    if m:
        return {"kind": "shuffle_hand_draw", "count": int(m[1])}
    if text in {
        "Switch your Active Pokémon with 1 of your Benched Pokémon.",
        "Switch 1 of your Active Pokémon with 1 of your Benched Pokémon.",
    }:
        return {"kind": "switch"}
    if card["name"] in {"Gust of Wind", "Boss's Orders"} and text in {
        "Choose 1 of your opponent's Benched Pokémon and switch it with his or her Active Pokémon.",
        "Switch 1 of your opponent's Benched Pokémon with their Active Pokémon.",
        "Switch in 1 of your opponent's Benched Pokémon to the Active Spot.",
    }:
        return {"kind": "gust"}
    if re.fullmatch(r"Search your deck for a basic Energy card, (?:reveal it|show it to your opponent), and put it into your hand\. (?:Shuffle your deck afterward|Then, shuffle your deck)\.", text):
        return {"kind": "search_energy", "count": 1}
    if re.fullmatch(r"Search your deck for up to 2 Basic Pokémon, reveal them, and put them into your hand\. (?:Shuffle your deck afterward|Then, shuffle your deck)\.", text):
        return {"kind": "search_basic", "count": 2}
    return None


def normalize_card(card: Any, data: dict | None = None) -> dict:
    """Normalize a Card ORM row plus enrichment, or one complete upstream dict."""
    complete = isinstance(card, dict) or data is not None
    if isinstance(card, dict):
        raw = deepcopy(data if data is not None else card)
        catalog_id = str(card.get("id", ""))
        fallback = {}
    else:
        raw = deepcopy(data or {})
        catalog_id = str(card.tcg_card_id)
        fallback = {
            "id": catalog_id, "name": card.name, "supertype": card.supertype,
            "hp": card.hp, "attacks": card.attacks, "subtypes": card.tcg_subtypes,
            "types": card.tcg_types, "evolvesFrom": card.evolves_from,
            "images": {"small": card.image_small_url, "large": card.image_large_url},
            "set": {"id": card.set_code, "name": card.set_name},
        }
    merged = {**fallback, **raw}
    out = {
        "id": catalog_id, "name": _text(merged.get("name")),
        "supertype": merged.get("supertype"), "subtypes": list(merged.get("subtypes") or []),
        "hp": _int(merged.get("hp")), "types": list(merged.get("types") or []),
        "evolves_from": merged.get("evolvesFrom"),
        "attacks": deepcopy(merged.get("attacks") or []),
        "abilities": deepcopy(merged.get("abilities") or []),
        "rules": [_text(r) for r in merged.get("rules") or []],
        "weaknesses": deepcopy(merged.get("weaknesses") or []),
        "resistances": deepcopy(merged.get("resistances") or []),
        "retreat_cost": list(merged.get("retreatCost") or []),
        "image_small": (merged.get("images") or {}).get("small", ""),
        "image_large": (merged.get("images") or {}).get("large", ""),
        "set_id": (merged.get("set") or {}).get("id", catalog_id.rsplit("-", 1)[0]),
        "set_name": (merged.get("set") or {}).get("name", ""),
        "legalities": deepcopy(merged.get("legalities") or {}),
        "definition_version": DEFINITION_VERSION, "prize_count": 1,
        "supported": False, "unsupported_reasons": [],
    }
    reasons = out["unsupported_reasons"]
    if not complete:
        reasons.append("Complete rules data has not been imported for this printing.")
    if raw.get("id", catalog_id) != catalog_id:
        reasons.append("Rules data does not match this catalog printing.")
    if not catalog_id or not out["name"]:
        reasons.append("Card identity is missing.")
    if raw.get("ancientTrait"):
        reasons.append("Ancient Trait effects are not implemented.")
    for ability in out["abilities"]:
        ability["text"] = _text(ability.get("text"))
        effect = _ability_effect(ability)
        if effect is None:
            reasons.append(f"Ability not implemented: {ability.get('name', 'unknown')}.")
        else:
            ability["effect"] = effect
    if out["supertype"] == "Pokémon":
        if out["hp"] <= 0 or not out["types"]:
            reasons.append("Pokémon HP or printed type is missing.")
        if not {"Basic", "Stage 1", "Stage 2"}.intersection(out["subtypes"]):
            reasons.append("This evolution or setup mechanic is not implemented.")
        if (set(out["subtypes"]) - POKEMON_TAGS):
            reasons.append("Card subtype mechanic not implemented: " + ", ".join(sorted(set(out["subtypes"]) - POKEMON_TAGS)) + ".")
        if "Basic" not in out["subtypes"] and not out["evolves_from"]:
            reasons.append("Evolution parent is missing.")
        for rule in out["rules"]:
            if rule in PRIZE_RULES:
                out["prize_count"] = max(out["prize_count"], PRIZE_RULES[rule])
            elif rule == UNLIMITED_COPIES_RULE:
                out["unlimited_copies"] = True
            elif rule == UNOWN_COPIES_RULE:
                out["unown_limit"] = True
            else:
                reasons.append("Rule effect not implemented: " + rule)
        for attack in out["attacks"]:
            attack["damage"] = str(attack.get("damage") or "")
            attack["cost"] = list(attack.get("cost") or [])
            if attack["cost"] == ["Free"]:
                attack["cost"] = []
            if set(attack["cost"]) - ENERGY_TYPES - {"Colorless"}:
                reasons.append("Attack Energy cost is not implemented.")
            attack["text"] = _text(attack.get("text"))
            effect = attack_effect(attack, out["name"])
            if effect is None:
                reasons.append(f"Attack effect not implemented: {attack.get('name', 'unknown')}.")
            else:
                attack["effect"] = effect
        if not out["attacks"]:
            reasons.append("No implemented attacks are available.")
        for relation, values in (("Weakness", out["weaknesses"]), ("Resistance", out["resistances"])):
            for value in values:
                if not re.fullmatch(r"(?:[×x]\d+|[+-]\d+)", str(value.get("value", ""))):
                    reasons.append(f"Unsupported printed {relation.lower()} value.")
    elif out["supertype"] == "Energy":
        energy_type = out["name"].removeprefix("Basic ").removesuffix(" Energy")
        if "Basic" in out["subtypes"] and energy_type in ENERGY_TYPES and not out["rules"]:
            out["effect"] = {"kind": "basic_energy", "type": energy_type}
            out["types"] = [energy_type]
        elif out["name"] == "Double Colorless Energy" and out["rules"] in [
            ["Double Colorless Energy provides ColorlessColorless Energy."],
            ["Provides Colorless Colorless energy. Doesn't count as a basic Energy card."],
            ["Provides ColorlessColorless energy. Doesn't count as a basic Energy card."],
        ]:
            out["effect"] = {"kind": "double_colorless"}
        else:
            reasons.append("Energy effect is not implemented.")
    elif out["supertype"] == "Trainer":
        # Tools remain separate from Items after the 2023 official errata.
        if not out["subtypes"]:
            out["subtypes"] = ["Item"]
        if "Pokémon Tool" in out["subtypes"]:
            out["subtypes"] = [tag for tag in out["subtypes"] if tag != "Item"]
        if set(out["subtypes"]) - {"Item", "Supporter", "Pokémon Tool", "Stadium", "Team Plasma", "Rapid Strike", "Single Strike", "Fusion Strike", "Ancient", "Future"}:
            reasons.append("Trainer subtype mechanic is not implemented.")
        effect = _trainer_effect(out)
        if effect is None:
            reasons.append("Trainer effect is not implemented.")
        else:
            out["effect"] = effect
        if out["name"] == "Potion":
            out["errata"] = {"source": ERRATA_SOURCE, "note": "All Potion printings heal 30 damage."}
    else:
        reasons.append("This catalog entry is not a playable Pokémon, Trainer, or Energy card.")
    out["supported"] = not reasons
    return out


def available_quantities(source: str, mixed: bool, mine: dict[str, int], theirs: dict[str, int] | None = None) -> dict[str, int] | None:
    """Each mixed participant independently receives the combined quantities."""
    if source not in {"global", "owned"}:
        raise ValueError("Deck source must be Global or Owned.")
    if source == "global":
        if mixed:
            raise ValueError("Mixed is available only with Owned decks.")
        return None
    if mixed and theirs is None:
        raise ValueError("An opponent must join before editing a Mixed deck.")
    quantities = Counter({str(k): max(0, int(v)) for k, v in mine.items()})
    if mixed:
        quantities.update({str(k): max(0, int(v)) for k, v in (theirs or {}).items()})
    return dict(quantities)


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).replace("’", "'").casefold().strip()


def validate_deck(entries: list[dict], definitions: dict[str, dict], available: dict[str, int] | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(entries, list) or len(entries) > 60:
        return ["A deck must contain at most 60 distinct printing entries."]
    counts: Counter = Counter()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("Every deck entry must identify a card and quantity.")
            continue
        quantity = entry.get("quantity")
        if type(quantity) is not int or not 1 <= quantity <= 60:
            errors.append("Card quantities must be whole numbers from 1 to 60.")
            continue
        counts[str(entry.get("card_id", ""))] += quantity
    total = sum(counts.values())
    if total != 60:
        errors.append(f"A deck must contain exactly 60 cards (currently {total}).")
    names: Counter = Counter()
    restricted: Counter = Counter()
    basic_count = 0
    unown_limit = any(definitions.get(card_id, {}).get("unown_limit") for card_id in counts)
    unown_count = 0
    for card_id, quantity in counts.items():
        card = definitions.get(card_id)
        if card is None:
            errors.append(f"Card {card_id} is not in the global catalog.")
            continue
        if not card.get("supported"):
            details = "; ".join(card.get("unsupported_reasons") or ["Rules are not implemented."])
            errors.append(f"{card['name']} ({card_id}) cannot be played: {details}")
        if available is not None and quantity > available.get(card_id, 0):
            errors.append(f"{card['name']} ({card_id}): {quantity} selected, {available.get(card_id, 0)} available in this collection pool.")
        subtypes = set(card.get("subtypes", []))
        if card.get("supertype") == "Pokémon" and "Basic" in subtypes:
            basic_count += quantity
        if card.get("supertype") == "Energy" and "Basic" in subtypes:
            continue
        name = _name_key(card["name"])
        if not card.get("unlimited_copies"):
            names[name] += quantity
        if card.get("supertype") == "Pokémon" and "Basic" in subtypes and "unown" in name:
            unown_count += quantity
        # These limits are enforced even while effect coverage blocks the card.
        for tag in ("ACE SPEC", "Radiant", "Star"):
            if tag in subtypes:
                restricted[tag] += quantity
        if "Prism Star" in subtypes:
            restricted["Prism Star:" + name] += quantity
    if not basic_count:
        errors.append("A deck must include at least one Basic Pokémon.")
    if unown_limit and unown_count > 4:
        errors.append("This Unown's printed rule allows at most 4 Basic Pokémon with Unown in their names.")
    for name, quantity in names.items():
        if quantity > 4:
            errors.append(f"At most 4 cards named {name} are allowed across all printings (currently {quantity}).")
    for rule, quantity in restricted.items():
        if quantity > 1:
            errors.append(f"At most 1 {rule} card is allowed (currently {quantity}).")
    return errors


def expand_deck(entries: list[dict]) -> list[str]:
    """Expand validated entries; duplicate-printing entries are intentionally additive."""
    if not isinstance(entries, list) or len(entries) > 60:
        raise ValueError("Invalid deck entries.")
    out = []
    for entry in entries:
        quantity = entry.get("quantity") if isinstance(entry, dict) else None
        if type(quantity) is not int or not 1 <= quantity <= 60:
            raise ValueError("Invalid card quantity.")
        out.extend([str(entry["card_id"])] * quantity)
        if len(out) > 60:
            raise ValueError("A deck cannot contain more than 60 cards.")
    return out


def coverage_report(definitions: dict[str, dict]) -> dict:
    reasons: Counter = Counter()
    by_type: dict[str, Counter] = {}
    for card in definitions.values():
        group = by_type.setdefault(card.get("supertype") or "Unknown", Counter())
        group["total"] += 1
        group["supported"] += int(bool(card.get("supported")))
        for reason in card.get("unsupported_reasons", []):
            reasons[reason.split(":", 1)[0]] += 1
    return {
        "version": DEFINITION_VERSION, "total": len(definitions),
        "supported": sum(bool(c.get("supported")) for c in definitions.values()),
        "by_type": {k: dict(v) for k, v in by_type.items()},
        "unsupported_reason_counts": dict(reasons.most_common()),
        "complete": all(c.get("supported") for c in definitions.values()) and bool(definitions),
    }


def starter_deck(definitions: dict[str, dict], available: dict[str, int] | None = None) -> list[dict]:
    """Build a legal mono-type starter using real, supported global printings."""
    cards = sorted((c for c in definitions.values() if c.get("supported") and
                    (available is None or available.get(c["id"], 0) > 0)), key=lambda c: c["id"])
    energies = {c["effect"]["type"]: c for c in cards if c.get("effect", {}).get("kind") == "basic_energy"}
    for energy_type in ("Water", "Fire", "Grass", "Lightning", "Fighting", "Psychic", "Darkness", "Metal", "Fairy"):
        if energy_type not in energies:
            continue
        basics = [c for c in cards if c["supertype"] == "Pokémon" and "Basic" in c["subtypes"] and c["prize_count"] == 1 and c["types"] == [energy_type] and all(set(a["cost"]) <= {energy_type, "Colorless"} for a in c["attacks"])]
        # Prefer substantial HP and inexpensive attacks to keep the starter usable.
        basics.sort(key=lambda c: (abs(c["hp"] - 100), min(len(a["cost"]) for a in c["attacks"]), c["id"]))
        chosen, names = [], set()
        for card in basics:
            if _name_key(card["name"]) not in names:
                chosen.append(card)
                names.add(_name_key(card["name"]))
            if len(chosen) == 4:
                break
        if len(chosen) < 4:
            continue
        count = lambda c, n: n if available is None else min(n, available.get(c["id"], 0))
        entries = [{"card_id": c["id"], "quantity": count(c, 4)} for c in chosen]
        for wanted in ("Professor's Research", "Hau", "Potion", "Switch", "Energy Search", "Youngster", "Boss's Orders"):
            match = next((c for c in cards if c["name"] == wanted and c["supertype"] == "Trainer"), None)
            if match:
                entries.append({"card_id": match["id"], "quantity": count(match, 4)})
        remaining = 60 - sum(e["quantity"] for e in entries)
        for energy in cards:
            if energy.get("effect") != {"kind": "basic_energy", "type": energy_type}:
                continue
            n = count(energy, remaining)
            if n:
                entries.append({"card_id": energy["id"], "quantity": n})
                remaining -= n
            if not remaining:
                break
        if not validate_deck(entries, definitions, available):
            return entries
    return []
