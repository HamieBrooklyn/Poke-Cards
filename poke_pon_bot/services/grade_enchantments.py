"""Random slab films rolled with a grade. Cooler films are rarer."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass

from poke_pon_bot.services.weighted_rng import weighted_choice


@dataclass(frozen=True)
class GradeEnchantment:
    code: str
    name: str
    weight: float
    rarity: str
    accent: tuple[int, int, int]
    film: tuple[int, int, int, int]


ENCHANTMENTS: tuple[GradeEnchantment, ...] = (
    GradeEnchantment("clear", "Clear Coat", 280, "common", (200, 210, 220), (255, 255, 255, 40)),
    GradeEnchantment("frost", "Frost Veil", 160, "uncommon", (160, 220, 255), (180, 230, 255, 70)),
    GradeEnchantment("ember", "Ember Sheen", 140, "uncommon", (255, 140, 70), (255, 120, 40, 65)),
    GradeEnchantment("aurora", "Aurora Film", 90, "rare", (120, 255, 200), (80, 255, 180, 70)),
    GradeEnchantment("holofoil", "Holofoil", 70, "rare", (255, 120, 220), (180, 120, 255, 75)),
    GradeEnchantment("storm", "Storm Glass", 45, "epic", (120, 180, 255), (90, 140, 255, 80)),
    GradeEnchantment("void", "Void Film", 30, "epic", (160, 80, 255), (40, 0, 70, 90)),
    GradeEnchantment("prism", "Prismatic", 18, "legendary", (255, 80, 200), (255, 80, 220, 80)),
    GradeEnchantment("celestial", "Celestial Gold", 10, "legendary", (255, 210, 80), (255, 200, 60, 85)),
    GradeEnchantment("mythic", "Mythic Flare", 4, "mythic", (255, 70, 90), (255, 40, 80, 95)),
)

_BY_CODE = {e.code: e for e in ENCHANTMENTS}
DEFAULT_ENCHANTMENT = ENCHANTMENTS[0]


def get_enchantment(code: str | None) -> GradeEnchantment | None:
    if not code:
        return None
    return _BY_CODE.get(str(code).strip().lower())


def enchantment_or_default(code: str | None) -> GradeEnchantment:
    return get_enchantment(code) or DEFAULT_ENCHANTMENT


def roll_enchantment(*, rng: random.Random | None = None) -> GradeEnchantment:
    r = rng or random.Random(secrets.randbits(128))
    items = [(e, float(e.weight)) for e in ENCHANTMENTS]
    return weighted_choice(r, items)


def enchantment_api_payload(code: str | None, *, graded: bool) -> dict | None:
    if not graded:
        return None
    e = enchantment_or_default(code)
    return {
        "code": e.code,
        "name": e.name,
        "rarity": e.rarity,
    }
