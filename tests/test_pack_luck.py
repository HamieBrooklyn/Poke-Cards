"""Pack-open luck scaling (price + code slot)."""

from poke_pon_bot.services.drops import (
    CODE_SLOT_LUCK_MULTIPLIER,
    PACK_OPEN_LUCK_PERCENT,
    pack_price_luck_bonus,
    pack_slot_luck_percent,
)


def test_pack_price_luck_bonus_scales_with_crystal_price() -> None:
    assert pack_price_luck_bonus(9) == 0.0
    assert pack_price_luck_bonus(11) == 4.0
    assert pack_price_luck_bonus(16) == 14.0


def test_code_slot_gets_double_pack_luck() -> None:
    main = pack_slot_luck_percent(crystal_price=11, code_slot=False)
    code = pack_slot_luck_percent(crystal_price=11, code_slot=True)
    assert main == PACK_OPEN_LUCK_PERCENT + 4.0
    assert code == main * CODE_SLOT_LUCK_MULTIPLIER
