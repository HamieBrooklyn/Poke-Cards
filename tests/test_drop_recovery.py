"""Channel drop persistence helpers."""

from poke_pon_bot.services.drop_recovery import _slot_maps_to_int


def test_slot_maps_roundtrip() -> None:
    claimer, pids = _slot_maps_to_int(
        {"0": 111, "3": 222},
        {"0": "CARD-AAA", "3": "CARD-BBB"},
    )
    assert claimer == {0: 111, 3: 222}
    assert pids == {0: "CARD-AAA", 3: "CARD-BBB"}
