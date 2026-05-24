"""Rarity luck curve for drops, packs, and wild duels (supports values below 0 and above 100)."""

from __future__ import annotations

from typing import Mapping

# Same tier curve as code-card slots in :mod:`poke_pon_bot.services.drops`.
RARITY_LUCK_PEAK_MULT: Mapping[int, float] = {
    1: 1.0,
    2: 1.04,
    3: 1.12,
    4: 1.28,
    5: 1.55,
    6: 1.85,
    7: 2.25,
    8: 2.75,
    9: 3.35,
    10: 4.0,
}


def luck_rarity_weight_multiplier(rarity_class_id: int, luck_percent: float) -> float:
    """
    Scale a rarity tier's weight.

    * **0** — neutral.
    * **+100** — full bias toward this tier being treated like a code-card slot.
    * **>100** — extrapolates beyond that (very rare-heavy at high tiers).
    * **<0** — biases *common* tiers (high tiers are suppressed).
    """
    if luck_percent == 0:
        return 1.0
    peak = float(RARITY_LUCK_PEAK_MULT.get(int(rarity_class_id), 1.0))
    t = float(luck_percent) / 100.0
    if t >= 0:
        return 1.0 + (peak - 1.0) * t
    inv = 1.0 / peak if peak > 0 else 1.0
    return 1.0 + (inv - 1.0) * (-t)


def combine_luck_percent(*parts: float) -> float:
    """Sum luck modifiers (global boost + per-command extras)."""
    return sum(float(p) for p in parts)
