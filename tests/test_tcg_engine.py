"""Physical-game outcomes and privacy, independent of website rendering."""
from copy import deepcopy
import random
import json

import pytest

from poke_pon_bot.services.tcg_catalog import normalize_card
from poke_pon_bot.services.tcg_engine import RuleError, new_game, apply_action, project_state


def basic(name="Testmon", hp=60, damage="30", **extra):
    return normalize_card({"id": name, "name": name, "supertype": "Pokémon", "subtypes": ["Basic"],
        "hp": str(hp), "types": ["Lightning"], "retreatCost": ["Colorless"],
        "attacks": [{"name": "Strike", "cost": ["Colorless"], "damage": damage}], **extra})


def energy():
    return normalize_card({"id": "energy", "name": "Lightning Energy", "supertype": "Energy", "subtypes": ["Basic"]})


def decks():
    cards = [basic(f"Testmon {i}") for i in range(4) for _ in range(4)] + [energy() for _ in range(44)]
    return {"1": deepcopy(cards), "2": deepcopy(cards)}


def started(seed=1):
    rng = random.Random(seed)
    state = new_game(decks(), ["1", "2"], rng)
    state = apply_action(state, state["coin_winner"], {"type": "choose_start", "first_player": "1"}, rng)
    while state["phase"] == "setup":
        choice = state.get("pending_choice")
        if choice:
            state = apply_action(state, choice["player"], {"type": "choose", "choice_id": choice["id"],
                "selected": [choice["options"][0]["value"]]}, rng)
            continue
        for player in ["1", "2"]:
            b = state["board"][player]
            if not b["active"]:
                c = next(c for c in b["hand"] if c["card"]["supertype"] == "Pokémon")
                state = apply_action(state, player, {"type": "setup", "active_uid": c["uid"]}, rng)
            if not state["board"][player]["setup_ready"]:
                state = apply_action(state, player, {"type": "setup_ready"}, rng)
            if state["pending_choice"] or state["phase"] != "setup":
                break
    return state, rng


def test_setup_and_viewer_privacy():
    state, _ = started()
    view = project_state(state, "1")
    assert state["phase"] == "playing" and state["turn_player"] == "1"
    assert len(state["board"]["1"]["prizes"]) == 6
    assert len(view["board"]["1"]["hand"]) >= 7
    assert isinstance(view["board"]["2"]["hand"], dict)
    wire = json.dumps(view)
    for p in ["1", "2"]:
        for c in state["board"][p]["deck"] + state["board"][p]["prizes"]:
            assert c["uid"] not in wire
    for c in state["board"]["2"]["hand"]:
        assert c["uid"] not in wire
    assert "original_decks" not in view
    with pytest.raises(RuleError):
        project_state(state, "3")


def test_face_down_setup_hides_identity_and_start_choice_authority():
    state = new_game(decks(), ["1", "2"], random.Random(1))
    with pytest.raises(RuleError):
        apply_action(state, next(p for p in state["players"] if p != state["coin_winner"]), {"type": "choose_start", "first_player": "1"})
    state = apply_action(state, state["coin_winner"], {"type": "choose_start", "first_player": "1"}, random.Random(1))
    c = next(c for c in state["board"]["1"]["hand"] if c["card"]["supertype"] == "Pokémon")
    state = apply_action(state, "1", {"type": "setup", "active_uid": c["uid"]})
    assert project_state(state, "2")["board"]["1"]["active"] == {"face_down": True}
    assert c["uid"] not in json.dumps(project_state(state, "2"))


def test_first_turn_energy_limit_and_energy_is_not_spent_by_attacking():
    state, rng = started()
    b = state["board"]["1"]
    c = next(c for c in b["hand"] if c["card"]["supertype"] == "Energy")
    state = apply_action(state, "1", {"type": "attach", "card_uid": c["uid"], "target_uid": b["active"]["uid"]}, rng)
    before = deepcopy(state)
    with pytest.raises(RuleError, match="already attached"):
        apply_action(state, "1", {"type": "attach", "card_uid": c["uid"], "target_uid": b["active"]["uid"]})
    with pytest.raises(RuleError, match="starting player"):
        apply_action(state, "1", {"type": "attack", "attack_index": 0})
    assert state == before
    state = apply_action(state, "1", {"type": "end_turn"}, rng)
    state = apply_action(state, "2", {"type": "end_turn"}, rng)
    state = apply_action(state, "1", {"type": "attack", "attack_index": 0}, rng)
    assert state["board"]["2"]["active"]["damage"] == 30
    assert state["board"]["1"]["active"]["attachments"][0]["uid"] == c["uid"]
    assert state["turn_player"] == "2"


def test_conditions_checkup_poison_paralysis_and_printed_weakness():
    state, rng = started()
    active = state["board"]["1"]["active"]
    active["statuses"] = ["poisoned", "paralyzed"]
    state = apply_action(state, "1", {"type": "end_turn"}, rng)
    assert state["board"]["1"]["active"]["damage"] == 10
    assert state["board"]["1"]["active"]["statuses"] == ["poisoned"]
    state = apply_action(state, "2", {"type": "end_turn"}, rng)
    active = state["board"]["1"]["active"]
    active["card"]["attacks"][0]["cost"] = []
    state["board"]["2"]["active"]["card"]["weaknesses"] = [{"type": "Lightning", "value": "×2"}]
    state = apply_action(state, "1", {"type": "attack", "attack_index": 0}, rng)
    assert state["board"]["2"]["active"] is None
    assert state["pending_choice"]["type"] == "prizes"
    assert not any("card" in o for o in state["pending_choice"]["options"])


def test_mandatory_choice_private_and_illegal_or_duplicate_options_rejected():
    state, rng = started()
    state["turn_number"] = 3
    state["board"]["1"]["active"]["card"]["attacks"][0].update(cost=[], damage="100")
    state = apply_action(state, "1", {"type": "attack", "attack_index": 0}, rng)
    choice = state["pending_choice"]
    assert "options" not in project_state(state, "2")["pending_choice"]
    with pytest.raises(RuleError):
        apply_action(state, "2", {"type": "choose", "choice_id": choice["id"], "selected": [0]})
    with pytest.raises(RuleError):
        apply_action(state, "1", {"type": "choose", "choice_id": "stale", "selected": [0]})
    state = apply_action(state, "1", {"type": "choose", "choice_id": choice["id"], "selected": [0]}, rng)
    assert state["phase"] == "finished" and state["winner"] == "1"


def test_search_choice_hides_deck_and_reveals_only_selected_card():
    state, rng = started()
    trainer = normalize_card({"id": "search", "name": "Energy Search", "supertype": "Trainer", "subtypes": ["Item"],
        "rules": ["Search your deck for a basic Energy card, reveal it, and put it into your hand. Then, shuffle your deck."]})
    assert trainer["supported"]
    state["board"]["1"]["hand"].append({"uid": "search1", "card": trainer})
    state = apply_action(state, "1", {"type": "play_trainer", "card_uid": "search1"}, rng)
    choice = state["pending_choice"]
    view = project_state(state, "2")
    assert "options" not in view["pending_choice"]
    selected = choice["options"][0]["value"]
    assert selected not in json.dumps(view)
    state = apply_action(state, "1", {"type": "choose", "choice_id": choice["id"], "selected": [selected]}, rng)
    assert any(c["uid"] == selected for c in state["board"]["1"]["hand"])
    assert any("reveals Lightning Energy" in e["message"] for e in state["log"])
    assert state["board"]["1"]["discard"][-1]["uid"] == "search1"


def test_two_clients_can_finish_using_only_projected_actions_and_choices():
    state, rng = started(8)
    priorities = {"setup": 0, "bench": 1, "setup_ready": 2, "attach": 3, "attack": 4, "end_turn": 5}
    for _ in range(400):
        if state["phase"] == "finished":
            break
        p = state["pending_choice"]["player"] if state.get("pending_choice") else state["turn_player"]
        view = project_state(state, p)
        if view["pending_choice"]:
            choice = view["pending_choice"]
            action = {"type": "choose", "choice_id": choice["id"], "selected": [o["value"] for o in choice["options"][:choice["min"]]]}
        else:
            actions = [a["action"] for a in view["legal_actions"] if a["action"]["type"] in priorities]
            # Prioritize powering the Active rather than always attaching to Bench.
            active_uid = view["board"][p]["active"]["uid"]
            actions.sort(key=lambda a: (priorities[a["type"]], a.get("target_uid", active_uid) != active_uid))
            action = actions[0]
        state = apply_action(state, p, action, rng)
    assert state["phase"] == "finished"
    assert state["winner"] in ("1", "2")
    assert "surrender" not in state["reason"].lower()


def put_bench(state, player, name="Benchmon"):
    pokemon = deepcopy(state["board"][player]["active"])
    pokemon.update(uid=f"bench-{player}", card=basic(name), damage=0, attachments=[], evolution=[], statuses=[])
    state["board"][player]["bench"].append(pokemon)
    return pokemon


def double_energy(uid):
    card = normalize_card({"id": uid, "name": "Double Colorless Energy", "supertype": "Energy",
        "subtypes": ["Special"], "rules": ["Double Colorless Energy provides ColorlessColorless Energy."]})
    assert card["supported"]
    return {"uid": uid, "card": card}


def resolve_prizes(state, rng):
    while state.get("pending_choice") and state["pending_choice"]["type"] == "prizes":
        choice = state["pending_choice"]
        state = apply_action(state, choice["player"], {"type": "choose", "choice_id": choice["id"],
            "selected": [o["value"] for o in choice["options"][:choice["min"]]]}, rng)
    return state


@pytest.mark.parametrize("selection", [["double-1"], ["double-1", "double-2"]])
def test_retreat_pays_energy_units_and_clears_conditions_only_on_benched_pokemon(selection):
    state, rng = started()
    b = state["board"]["1"]
    active_uid = b["active"]["uid"]
    b["active"]["card"]["retreat_cost"] = ["Colorless"] * 2
    b["active"]["attachments"] = [double_energy("double-1"), double_energy("double-2")]
    b["active"]["damage"] = 20
    b["active"]["statuses"] = ["confused", "poisoned", "burned"]
    target = put_bench(state, "1")
    state = apply_action(state, "1", {"type": "retreat", "target_uid": target["uid"], "energy_uids": selection}, rng)
    b = state["board"]["1"]
    previous = next(p for p in b["bench"] if p["uid"] == active_uid)
    assert previous["damage"] == 20 and previous["statuses"] == []
    assert len(previous["attachments"]) == 2 - len(selection)
    assert {c["uid"] for c in b["discard"]} >= set(selection)
    with pytest.raises(RuleError, match="already retreated"):
        apply_action(state, "1", {"type": "retreat", "target_uid": active_uid, "energy_uids": []}, rng)


def test_paralysis_blocks_retreat_but_switch_item_works():
    state, rng = started()
    state["board"]["1"]["active"]["statuses"] = ["paralyzed"]
    target = put_bench(state, "1")
    with pytest.raises(RuleError, match="prevents retreat"):
        apply_action(state, "1", {"type": "retreat", "target_uid": target["uid"], "energy_uids": []}, rng)
    switch = normalize_card({"id": "switch", "name": "Switch", "supertype": "Trainer", "subtypes": ["Item"],
        "rules": ["Switch your Active Pokémon with 1 of your Benched Pokémon."]})
    state["board"]["1"]["hand"].append({"uid": "switch", "card": switch})
    state = apply_action(state, "1", {"type": "play_trainer", "card_uid": "switch", "target_uid": target["uid"]}, rng)
    assert state["board"]["1"]["active"]["uid"] == target["uid"]
    assert state["board"]["1"]["bench"][0]["statuses"] == []


def test_evolution_timing_preserves_damage_and_energy_and_removes_conditions():
    state, rng = started()
    active = state["board"]["1"]["active"]
    original_uid = active["uid"]
    evolution = basic("Evolved", hp=120, subtypes=["Stage 1"], evolvesFrom=active["card"]["name"])
    state["board"]["1"]["hand"].append({"uid": "evolution", "card": evolution})
    with pytest.raises(RuleError, match="first turn"):
        apply_action(state, "1", {"type": "evolve", "card_uid": "evolution", "target_uid": original_uid}, rng)
    state = apply_action(state, "1", {"type": "end_turn"}, rng)
    state = apply_action(state, "2", {"type": "end_turn"}, rng)
    state["board"]["1"]["active"].update(damage=20, statuses=["paralyzed", "poisoned"], attachments=[double_energy("attached")])
    state = apply_action(state, "1", {"type": "evolve", "card_uid": "evolution", "target_uid": original_uid}, rng)
    active = state["board"]["1"]["active"]
    assert active["damage"] == 20 and active["statuses"] == []
    assert active["attachments"][0]["uid"] == "attached"
    assert active["evolution"][0]["uid"] == original_uid
    second = basic("Final", hp=150, subtypes=["Stage 2"], evolvesFrom="Evolved")
    state["board"]["1"]["hand"].append({"uid": "second", "card": second})
    with pytest.raises(RuleError, match="cannot evolve again"):
        apply_action(state, "1", {"type": "evolve", "card_uid": "second", "target_uid": "evolution"}, rng)


def test_supporter_first_player_restriction_once_per_turn_and_item_unlimited():
    state, rng = started()
    trainer = normalize_card({"id": "supporter", "name": "Draw Supporter", "supertype": "Trainer",
        "subtypes": ["Supporter"], "rules": ["Draw 3 cards."]})
    for p in ["1", "2"]:
        state["board"][p]["hand"].extend({"uid": f"supporter-{p}-{i}", "card": deepcopy(trainer)} for i in range(2))
    with pytest.raises(RuleError, match="starting player"):
        apply_action(state, "1", {"type": "play_trainer", "card_uid": "supporter-1-0"}, rng)
    state = apply_action(state, "1", {"type": "end_turn"}, rng)
    size = len(state["board"]["2"]["hand"])
    state = apply_action(state, "2", {"type": "play_trainer", "card_uid": "supporter-2-0"}, rng)
    assert len(state["board"]["2"]["hand"]) == size + 2
    with pytest.raises(RuleError, match="already played"):
        apply_action(state, "2", {"type": "play_trainer", "card_uid": "supporter-2-1"}, rng)


def test_drawing_last_card_by_effect_does_not_lose_until_mandatory_turn_draw():
    state, rng = started()
    b = state["board"]["1"]
    b["deck"] = b["deck"][:1]
    trainer = normalize_card({"id": "draw", "name": "Draw Item", "supertype": "Trainer",
        "subtypes": ["Item"], "rules": ["Draw 3 cards."]})
    b["hand"].append({"uid": "draw", "card": trainer})
    size = len(b["hand"])
    state = apply_action(state, "1", {"type": "play_trainer", "card_uid": "draw"}, rng)
    assert len(state["board"]["1"]["hand"]) == size
    assert state["phase"] == "playing" and not state["board"]["1"]["deck"]
    state = apply_action(state, "1", {"type": "end_turn"}, rng)
    state = apply_action(state, "2", {"type": "end_turn"}, rng)
    assert state["phase"] == "finished" and state["winner"] == "2"
    assert "could not draw" in state["reason"]


def test_simultaneous_knockouts_next_player_promotes_first():
    state, rng = started()
    state["turn_number"] = 3
    for p in ["1", "2"]:
        put_bench(state, p)
    state["board"]["1"]["active"]["card"]["attacks"][0].update(cost=[], damage="60", effect={"kind": "recoil", "amount": 60})
    state = apply_action(state, "1", {"type": "attack", "attack_index": 0}, rng)
    state = resolve_prizes(state, rng)
    assert state["pending_choice"]["type"] == "promote"
    assert state["pending_choice"]["player"] == "2"
    assert len(state["board"]["1"]["prizes"]) == len(state["board"]["2"]["prizes"]) == 5
    choice = state["pending_choice"]
    state = apply_action(state, "2", {"type": "choose", "choice_id": choice["id"], "selected": ["bench-2"]}, rng)
    assert state["pending_choice"]["player"] == "1"


def test_equal_simultaneous_win_conditions_restart_a_six_prize_tiebreaker():
    state, rng = started()
    state["turn_number"] = 3
    state["board"]["1"]["active"]["card"]["attacks"][0].update(cost=[], damage="60", effect={"kind": "recoil", "amount": 60})
    state = apply_action(state, "1", {"type": "attack", "attack_index": 0}, rng)
    state = resolve_prizes(state, rng)
    assert state["phase"] == "choose_start" and state["tiebreaker"]
    assert state["winner"] is None
    assert all(len(state["board"][p]["deck"]) == 60 for p in ["1", "2"])
    assert any("six-Prize tiebreaker" in e["message"] for e in state["log"])


def test_two_win_conditions_beat_one_in_simultaneous_knockouts():
    state, rng = started()
    state["turn_number"] = 3
    state["board"]["1"]["prizes"] = state["board"]["1"]["prizes"][:1]
    state["board"]["1"]["active"]["card"]["attacks"][0].update(cost=[], damage="60", effect={"kind": "recoil", "amount": 60})
    state = apply_action(state, "1", {"type": "attack", "attack_index": 0}, rng)
    state = resolve_prizes(state, rng)
    assert state["phase"] == "finished" and state["winner"] == "1"
