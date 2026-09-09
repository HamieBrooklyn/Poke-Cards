"""Executable catalog coverage must fail closed and preserve deck access limits."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from poke_pon_bot.services.tcg_catalog import (
    available_quantities, coverage_report, expand_deck, normalize_card,
    starter_deck, validate_deck,
)


def pokemon(card_id="test-1", name="Pikachu", **changes):
    raw = {"id": card_id, "name": name, "supertype": "Pokémon", "subtypes": ["Basic"], "hp": "60", "types": ["Lightning"], "attacks": [{"name": "Gnaw", "damage": "10", "cost": ["Colorless"], "text": ""}]}
    raw.update(changes)
    return normalize_card(raw)


def energy(card_id="test-2"):
    return normalize_card({"id": card_id, "name": "Lightning Energy", "supertype": "Energy", "subtypes": ["Basic"]})


def test_complete_normalization_and_unknown_effects_fail_closed():
    plain = pokemon()
    assert plain["supported"]
    assert plain["attacks"][0]["effect"] == {"kind": "damage"}
    variable = pokemon(attacks=[{"name": "Variable", "damage": "30+", "text": "", "cost": []}])
    assert not variable["supported"]
    unknown = pokemon(attacks=[{"name": "Draw", "damage": "", "text": "Draw 2 cards. Your opponent loses the game.", "cost": []}])
    assert not unknown["supported"]
    ability = pokemon(abilities=[{"name": "Secret", "text": "Draw 20 cards."}])
    assert not ability["supported"]


def test_printing_rules_do_not_accept_wrong_enrichment_or_incomplete_legacy_data():
    row = SimpleNamespace(tcg_card_id="test-1", name="Pikachu", supertype="Pokémon", hp="60", attacks=[], tcg_subtypes=["Basic"], tcg_types=["Lightning"], evolves_from=None, image_small_url="", image_large_url="", set_code="test", set_name="Test")
    assert not normalize_card(row)["supported"]
    assert any("does not match" in r for r in normalize_card(row, {"id": "wrong"})["unsupported_reasons"])


@pytest.mark.parametrize("text,damage,effect", [
    ("Flip 2 coins. This attack does 20 damage times the number of heads.", "20×", {"kind": "flip_damage", "count": 2, "amount": 20}),
    ("Flip a coin. If heads, this attack does 20 more damage.", "30+", {"kind": "flip_bonus", "amount": 20}),
    ("Flip a coin. If heads, the Defending Pokémon is now Paralyzed.", "10", {"kind": "flip_condition", "status": "paralyzed"}),
    ("Discard 2 Fire Energy from this Pokémon.", "100", {"kind": "discard_self_energy", "count": 2, "type": "Fire"}),
    ("Heal 30 damage from this Pokémon.", "10", {"kind": "heal_self", "amount": 30}),
    ("Flip a coin. If tails, this attack does nothing.", "60", {"kind": "flip_fail"}),
])
def test_exact_effect_templates(text, damage, effect):
    card = pokemon(attacks=[{"name": "Move", "damage": damage, "text": text, "cost": []}])
    assert card["supported"]
    assert card["attacks"][0]["effect"] == effect


def test_compound_effects_keep_conditional_scope():
    card = pokemon(attacks=[{"name": "Move", "damage": "10", "text": "Draw 2 cards. Heal 30 damage from this Pokémon."}])
    assert card["supported"]
    assert len(card["attacks"][0]["effect"]) == 2
    conditional = pokemon(attacks=[{"name": "Move", "damage": "10", "text": "Flip a coin. If heads, draw 2 cards. Heal 30 damage from this Pokémon."}])
    assert not conditional["supported"]


def test_known_prize_rules_and_advanced_mechanics():
    assert pokemon(name="Pikachu V", subtypes=["Basic", "V"], rules=["V rule: When your Pokémon V is Knocked Out, your opponent takes 2 Prize cards."])["prize_count"] == 2
    assert not pokemon(subtypes=["Basic", "Tera"])["supported"]
    assert not pokemon(ancientTrait={"name": "Growth", "text": "More energy"})["supported"]


def test_potion_errata_applies_and_unimplemented_tools_block():
    card = normalize_card({"id": "base1-94", "name": "Potion", "supertype": "Trainer", "rules": ["Remove up to 2 damage counters from 1 of your Pokémon."]})
    assert card["supported"]
    assert card["effect"] == {"kind": "heal", "amount": 30}
    assert "errata" in card
    tool = normalize_card({"id": "tool-1", "name": "Magic", "supertype": "Trainer", "subtypes": ["Item", "Pokémon Tool"], "rules": ["Draw 3 cards."]})
    assert not tool["supported"]


def test_tool_stadium_and_ability_normalization_consumes_all_printed_conditions():
    raw = {"id": "swsh1-156", "name": "Air Balloon", "supertype": "Trainer", "subtypes": ["Item", "Pokémon Tool"],
           "rules": ["Attach a Pokémon Tool to 1 of your Pokémon that doesn't already have a Pokémon Tool attached.",
                     "The Retreat Cost of the Pokémon this card is attached to is ColorlessColorless less.",
                     "You may play any number of Item cards during your turn."]}
    tool = normalize_card(raw)
    assert tool["supported"] and tool["subtypes"] == ["Pokémon Tool"]
    assert tool["effect"] == {"kind": "tool", "retreat_reduction": 2}
    raw["rules"][1] += " You can retreat while Paralyzed."
    assert not normalize_card(raw)["supported"]
    for ability_type in ("Poké-Power", "Pokémon Power", "Ability"):
        card = pokemon(abilities=[{"type": ability_type, "name": "Draw", "text": "Once during your turn, you may draw a card."}])
        assert card["supported"] is (ability_type == "Ability")
    card = pokemon(abilities=[{"type": "Ability", "name": "Draw", "text": "Once during your turn, you may draw a card. If you use this Ability, your turn ends."}])
    assert not card["supported"]
    stadium = normalize_card({"id": "stadium", "name": "Rough Seas", "supertype": "Trainer", "subtypes": ["Stadium"],
        "rules": ["Once during each player's turn, that player may heal 30 damage from each of his or her Water Pokémon and Lightning Pokémon.",
                  "This card stays in play when you play it. Discard this card if another Stadium card comes into play. If another card with the same name is in play, you can't play this card."]})
    assert stadium["supported"] and stadium["effect"]["heal_types"] == ["Water", "Lightning"]


def test_sixty_cards_basic_and_ownership_even_for_basic_energy():
    definitions = {"test-1": pokemon(), "test-2": energy()}
    entries = [{"card_id": "test-1", "quantity": 4}, {"card_id": "test-2", "quantity": 56}]
    assert not validate_deck(entries, definitions)
    assert len(expand_deck(entries)) == 60
    assert any("55 available" in e for e in validate_deck(entries, definitions, {"test-1": 4, "test-2": 55}))
    assert any("Basic Pokémon" in e for e in validate_deck([{"card_id": "test-2", "quantity": 60}], definitions))
    assert any("exactly 60" in e for e in validate_deck(entries[:1], definitions))


def test_copy_limits_sum_duplicate_entries_and_printings():
    definitions = {"test-1": pokemon(), "test-2": energy(), "test-3": pokemon("test-3")}
    entries = [{"card_id": "test-1", "quantity": 2}, {"card_id": "test-1", "quantity": 2}, {"card_id": "test-3", "quantity": 1}, {"card_id": "test-2", "quantity": 55}]
    assert any("At most 4" in e for e in validate_deck(entries, definitions))
    assert any("4 selected, 3 available" in e for e in validate_deck(entries, definitions, {"test-1": 3, "test-3": 1, "test-2": 55}))


@pytest.mark.parametrize("quantity", [True, False, 1.5, "4", 0, -1, 61])
def test_quantities_are_bounded_integers(quantity):
    entries = [{"card_id": "test-1", "quantity": quantity}]
    assert any("whole numbers" in e for e in validate_deck(entries, {"test-1": pokemon()}))
    with pytest.raises(ValueError):
        expand_deck(entries)


def test_mixed_virtual_access_is_independent_and_never_modifies_inventory():
    mine, theirs = {"x": 2, "energy": 5}, {"x": 3, "y": 1}
    original = deepcopy((mine, theirs))
    assert available_quantities("global", False, mine) is None
    assert available_quantities("owned", False, mine) == mine
    assert available_quantities("owned", True, mine, theirs) == {"x": 5, "energy": 5, "y": 1}
    assert available_quantities("owned", True, theirs, mine) == {"x": 5, "energy": 5, "y": 1}
    assert (mine, theirs) == original
    with pytest.raises(ValueError, match="opponent"):
        available_quantities("owned", True, mine)
    with pytest.raises(ValueError, match="only"):
        available_quantities("global", True, mine, theirs)


def test_deck_construction_exceptions_and_shared_restrictions():
    arceus = pokemon("test-arceus", "Arceus", rules=["You may have as many of this card in your deck as you like."])
    assert arceus["supported"]
    definitions = {arceus["id"]: arceus, "test-2": energy()}
    assert not validate_deck([{"card_id": arceus["id"], "quantity": 20}, {"card_id": "test-2", "quantity": 40}], definitions)
    a = pokemon("test-a", "Radiant Alpha", subtypes=["Basic", "Radiant"])
    b = pokemon("test-b", "Radiant Beta", subtypes=["Basic", "Radiant"])
    definitions.update({a["id"]: a, b["id"]: b})
    errors = validate_deck([{"card_id": "test-a", "quantity": 1}, {"card_id": "test-b", "quantity": 1}, {"card_id": "test-2", "quantity": 58}], definitions)
    assert any("At most 1 Radiant" in e for e in errors)


def test_coverage_reports_gaps_and_starter_is_legal():
    definitions = {"test-2": energy()}
    for index in range(4):
        card = pokemon(f"basic-{index}", f"Basic {index}")
        definitions[card["id"]] = card
    unsupported = pokemon("unsupported", "Mystery", abilities=[{"name": "Unknown"}])
    definitions[unsupported["id"]] = unsupported
    deck = starter_deck(definitions)
    assert deck and not validate_deck(deck, definitions)
    assert all(definitions[e["card_id"]]["supported"] for e in deck)
    report = coverage_report(definitions)
    assert report["total"] == 6 and report["supported"] == 5
    assert not report["complete"]
    assert not coverage_report({})["complete"]
