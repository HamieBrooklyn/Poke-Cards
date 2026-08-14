"""Grade rarity bump: high grades step up the ladder; low grades never drop."""

from types import SimpleNamespace

from poke_pon_bot.services.grading import apply_grade_rarity, grade_rarity_bump


def _rc(name: str, order: int, *, id_: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=id_ or order, code=name.lower(), display_name=name, sort_order=order)


LADDER = [
    _rc("Common", 1),
    _rc("Uncommon", 2),
    _rc("Rare", 3),
    _rc("Rare Holo", 4),
    _rc("Ultra Rare", 5),
    _rc("Double Rare", 6),
    _rc("Illustration Rare", 7),
    _rc("Special Rare", 8),
    _rc("Hyper Rare", 9),
    _rc("Chase", 10),
]


def test_grade_8_bumps_uncommon_to_rare() -> None:
    assert grade_rarity_bump(8) == 1
    uncommon = LADDER[1]
    out = apply_grade_rarity(uncommon, 8, LADDER)
    assert out is not None
    assert out.display_name == "Rare"


def test_grade_6_does_not_drop_or_raise() -> None:
    assert grade_rarity_bump(6) == 0
    uncommon = LADDER[1]
    out = apply_grade_rarity(uncommon, 6, LADDER)
    assert out is uncommon


def test_grade_10_caps_at_chase() -> None:
    hyper = LADDER[8]
    out = apply_grade_rarity(hyper, 10, LADDER)
    assert out is not None
    assert out.display_name == "Chase"
