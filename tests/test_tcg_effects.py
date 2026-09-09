"""Persistent effects and Abilities exercise outcomes using normalized card text."""
import pytest

from test_tcg_engine import basic, energy, started, put_bench
from poke_pon_bot.services.tcg_catalog import normalize_card
from poke_pon_bot.services.tcg_engine import RuleError, new_game, apply_action, project_state, _validate_card


def trainer(state, uid, name, subtype, rule, player="1"):
    card = normalize_card({"id": uid, "name": name, "supertype": "Trainer", "subtypes": [subtype], "rules": [rule]})
    assert card["supported"], card["unsupported_reasons"]
    _validate_card(card)
    state["board"][player]["hand"].append({"uid": uid, "card": card})


def ability(pokemon, text, name="Draw"):
    card = basic(pokemon["card"]["name"], hp=120, abilities=[{"type": "Ability", "name": name, "text": text}])
    assert card["supported"], card["unsupported_reasons"]
    _validate_card(card)
    pokemon["card"] = card


def test_engine_and_workshop_agree_on_printed_unlimited_copy_exception():
    arceus = basic("Arceus", rules=["You may have as many of this card in your deck as you like."])
    deck = [arceus] * 20 + [energy()] * 40
    assert new_game({"1": deck, "2": deck}, ["1", "2"])["phase"] == "choose_start"


def test_tool_hp_scope_loss_on_evolution_causes_knockout():
    state, rng = started()
    uid = state["board"]["1"]["active"]["uid"]
    trainer(state, "cape", "Cape of Toughness", "Pokémon Tool", "The Basic Pokémon this card is attached to gets +50 HP, except Pokémon-GX.")
    state = apply_action(state, "1", {"type": "play_trainer", "card_uid": "cape", "target_uid": uid}, rng)
    assert project_state(state, "2")["board"]["1"]["active"]["effective_hp"] == 110
    trainer(state, "charm", "Big Charm", "Pokémon Tool", "The Pokémon this card is attached to gets +30 HP.")
    with pytest.raises(RuleError, match="already has"):
        apply_action(state, "1", {"type": "play_trainer", "card_uid": "charm", "target_uid": uid}, rng)
    state = apply_action(state, "1", {"type": "end_turn"}, rng)
    state = apply_action(state, "2", {"type": "end_turn"}, rng)
    active = state["board"]["1"]["active"]
    active["damage"] = 90
    evolution = basic("Evolved", hp=80, subtypes=["Stage 1"], evolvesFrom=active["card"]["name"])
    state["board"]["1"]["hand"].append({"uid": "evolve", "card": evolution})
    state = apply_action(state, "1", {"type": "evolve", "card_uid": "evolve", "target_uid": uid}, rng)
    assert state["board"]["1"]["active"] is None
    assert {c["uid"] for c in state["board"]["1"]["discard"]} >= {"cape", uid, "evolve"}
    assert state["pending_choice"]["player"] == "2"


def test_tool_damage_bonus_precedes_weakness_and_zero_damage_stays_zero():
    state, rng = started()
    uid = state["board"]["1"]["active"]["uid"]
    trainer(state, "band", "Muscle Band", "Pokémon Tool", "The attacks of the Pokémon this card is attached to do 20 more damage to your opponent's Active Pokémon (before applying Weakness and Resistance).")
    state = apply_action(state, "1", {"type": "play_trainer", "card_uid": "band", "target_uid": uid}, rng)
    state["turn_number"] = 3
    state["board"]["1"]["active"]["card"]["attacks"][0]["cost"] = []
    state["board"]["2"]["active"]["card"].update(hp=200, weaknesses=[{"type": "Lightning", "value": "×2"}])
    state = apply_action(state, "1", {"type": "attack", "attack_index": 0}, rng)
    assert state["board"]["2"]["active"]["damage"] == 100
    assert state["board"]["1"]["active"]["attachments"][0]["uid"] == "band"
    state = apply_action(state, "2", {"type": "end_turn"}, rng)
    state["board"]["1"]["active"]["card"]["attacks"][0]["damage"] = "0"
    state = apply_action(state, "1", {"type": "attack", "attack_index": 0}, rng)
    assert state["board"]["2"]["active"]["damage"] == 100


def test_float_stone_free_retreat_does_not_cure_paralysis():
    state, rng = started()
    uid = state["board"]["1"]["active"]["uid"]
    bench = put_bench(state, "1")
    trainer(state, "stone", "Float Stone", "Pokémon Tool", "The Pokémon this card is attached to has no Retreat Cost.")
    state = apply_action(state, "1", {"type": "play_trainer", "card_uid": "stone", "target_uid": uid}, rng)
    assert project_state(state, "1")["board"]["1"]["active"]["effective_retreat_cost"] == 0
    action = {"type": "retreat", "target_uid": bench["uid"], "energy_uids": []}
    state["board"]["1"]["active"]["statuses"] = ["paralyzed"]
    with pytest.raises(RuleError, match="prevents retreat"):
        apply_action(state, "1", action)
    state["board"]["1"]["active"]["statuses"] = []
    assert action in [a["action"] for a in project_state(state, "1")["legal_actions"]]
    state = apply_action(state, "1", action, rng)
    assert state["board"]["1"]["bench"][0]["attachments"][0]["uid"] == "stone"


def test_stadium_shared_healing_limits_and_replacement_owner():
    state, rng = started()
    seas = "Once during each player's turn, that player may heal 30 damage from each of his or her Water Pokémon and Lightning Pokémon."
    court = "The Retreat Cost of each Basic Pokémon in play (both yours and your opponent's) is Colorless less."
    trainer(state, "seas", "Rough Seas", "Stadium", seas)
    state["board"]["1"]["active"]["damage"] = 40
    bench = put_bench(state, "1")
    bench["card"]["types"], bench["damage"] = ["Fire"], 30
    state = apply_action(state, "1", {"type": "play_trainer", "card_uid": "seas"}, rng)
    state = apply_action(state, "1", {"type": "use_stadium"}, rng)
    assert state["board"]["1"]["active"]["damage"] == 10
    assert state["board"]["1"]["bench"][0]["damage"] == 30
    with pytest.raises(RuleError):
        apply_action(state, "1", {"type": "use_stadium"})
    trainer(state, "court", "Beach Court", "Stadium", court)
    with pytest.raises(RuleError, match="already played"):
        apply_action(state, "1", {"type": "play_trainer", "card_uid": "court"})
    state = apply_action(state, "1", {"type": "end_turn"}, rng)
    trainer(state, "same-seas", "Rough Seas", "Stadium", seas, "2")
    with pytest.raises(RuleError, match="name is already"):
        apply_action(state, "2", {"type": "play_trainer", "card_uid": "same-seas"})
    state["board"]["2"]["active"]["damage"] = 30
    state = apply_action(state, "2", {"type": "use_stadium"}, rng)
    assert state["board"]["2"]["active"]["damage"] == 0
    trainer(state, "court2", "Beach Court", "Stadium", court, "2")
    state = apply_action(state, "2", {"type": "play_trainer", "card_uid": "court2"}, rng)
    assert state["stadium"]["owner"] == "2"
    assert state["board"]["1"]["discard"][-1]["uid"] == "seas"
    for p in ("1", "2"):
        assert project_state(state, p)["board"][p]["active"]["effective_retreat_cost"] == 0


def test_abilities_per_pokemon_active_only_and_draw_to_limit():
    state, rng = started()
    active, bench = state["board"]["1"]["active"], put_bench(state, "1")
    for source in (active, bench):
        ability(source, "Once during your turn, you may draw a card.")
    count = len(state["board"]["1"]["hand"])
    for source in (active, bench):
        action = {"type": "ability", "pokemon_uid": source["uid"], "ability_index": 0}
        assert action in [a["action"] for a in project_state(state, "1")["legal_actions"]]
        state = apply_action(state, "1", action, rng)
        with pytest.raises(RuleError, match="already used"):
            apply_action(state, "1", action, rng)
    assert len(state["board"]["1"]["hand"]) == count + 2
    state = apply_action(state, "1", {"type": "end_turn"}, rng)
    state = apply_action(state, "2", {"type": "end_turn"}, rng)
    bench = state["board"]["1"]["bench"][0]
    ability(bench, "Once during your turn, if this Pokémon is in the Active Spot, you may draw a card.")
    with pytest.raises(RuleError, match="Active Spot"):
        apply_action(state, "1", {"type": "ability", "pokemon_uid": bench["uid"], "ability_index": 0})
    active = state["board"]["1"]["active"]
    ability(active, "Once during your turn, you may draw cards until you have 5 cards in your hand.")
    state["board"]["1"]["hand"] = state["board"]["1"]["hand"][:2]
    state = apply_action(state, "1", {"type": "ability", "pokemon_uid": active["uid"], "ability_index": 0}, rng)
    assert len(state["board"]["1"]["hand"]) == 5


@pytest.mark.parametrize("frequency", ["As often as you like", "Once"])
def test_energy_ability_target_and_frequency_do_not_consume_normal_attachment(frequency):
    state, rng = started()
    active, bench = state["board"]["1"]["active"], put_bench(state, "1")
    ability(active, frequency + " during your turn, you may attach a Lightning Energy card from your hand to 1 of your Benched Lightning Pokémon.")
    for i in range(3):
        state["board"]["1"]["hand"].append({"uid": f"extra{i}", "card": energy()})
    action = {"type": "ability", "pokemon_uid": active["uid"], "ability_index": 0, "card_uid": "extra0", "target_uid": active["uid"]}
    with pytest.raises(RuleError, match="eligible"):
        apply_action(state, "1", action)
    action["target_uid"] = bench["uid"]
    state = apply_action(state, "1", action, rng)
    action["card_uid"] = "extra1"
    if frequency == "Once":
        with pytest.raises(RuleError, match="already used"):
            apply_action(state, "1", action, rng)
    else:
        state = apply_action(state, "1", action, rng)
    state = apply_action(state, "1", {"type": "attach", "card_uid": "extra2", "target_uid": active["uid"]}, rng)
    assert len(state["board"]["1"]["bench"][0]["attachments"]) == (1 if frequency == "Once" else 2)
    assert len(state["board"]["1"]["active"]["attachments"]) == 1
