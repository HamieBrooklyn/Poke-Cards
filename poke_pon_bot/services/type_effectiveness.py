"""Pokémon video-game style type chart for duel damage (TCG type names → chart keys)."""

from __future__ import annotations

import math
from collections.abc import Sequence

# Chart keys = main-series types used in the effectiveness matrix.
ALL_TYPES: frozenset[str] = frozenset(
    {
        "Normal",
        "Fire",
        "Water",
        "Electric",
        "Grass",
        "Ice",
        "Fighting",
        "Poison",
        "Ground",
        "Flying",
        "Psychic",
        "Bug",
        "Rock",
        "Ghost",
        "Dragon",
        "Dark",
        "Steel",
        "Fairy",
    },
)

# Pokémon TCG API strings → chart keys
_RAW_TO_CHART: dict[str, str] = {
    "Colorless": "Normal",
    "Lightning": "Electric",
    "Metal": "Steel",
    "Darkness": "Dark",
}

# Chart keys → common TCG print names (for log text)
_CHART_TO_TCG_DISPLAY: dict[str, str] = {
    "Normal": "Colorless",
    "Electric": "Lightning",
    "Steel": "Metal",
    "Dark": "Darkness",
}

# Attack type → defending type → multiplier (omit pairs that are 1.0). Gen 6+ including Fairy.
_ATK_VS_DEF: dict[str, dict[str, float]] = {
    "Normal": {"Rock": 0.5, "Ghost": 0.0, "Steel": 0.5},
    "Fire": {
        "Fire": 0.5,
        "Water": 0.5,
        "Grass": 2.0,
        "Ice": 2.0,
        "Bug": 2.0,
        "Steel": 2.0,
        "Rock": 0.5,
        "Dragon": 0.5,
    },
    "Water": {
        "Fire": 2.0,
        "Water": 0.5,
        "Grass": 0.5,
        "Ground": 2.0,
        "Rock": 2.0,
        "Dragon": 0.5,
    },
    "Electric": {
        "Water": 2.0,
        "Electric": 0.5,
        "Grass": 0.5,
        "Ground": 0.0,
        "Flying": 2.0,
        "Dragon": 0.5,
    },
    "Grass": {
        "Fire": 0.5,
        "Water": 2.0,
        "Grass": 0.5,
        "Ground": 2.0,
        "Rock": 2.0,
        "Flying": 0.5,
        "Poison": 0.5,
        "Bug": 0.5,
        "Steel": 0.5,
        "Dragon": 0.5,
    },
    "Ice": {
        "Fire": 0.5,
        "Water": 0.5,
        "Grass": 2.0,
        "Ice": 0.5,
        "Ground": 2.0,
        "Flying": 2.0,
        "Dragon": 2.0,
        "Steel": 0.5,
    },
    "Fighting": {
        "Normal": 2.0,
        "Ice": 2.0,
        "Rock": 2.0,
        "Dark": 2.0,
        "Steel": 2.0,
        "Poison": 0.5,
        "Flying": 0.5,
        "Psychic": 0.5,
        "Bug": 0.5,
        "Fairy": 0.5,
        "Ghost": 0.0,
    },
    "Poison": {
        "Grass": 2.0,
        "Fairy": 2.0,
        "Poison": 0.5,
        "Ground": 0.5,
        "Rock": 0.5,
        "Ghost": 0.5,
        "Steel": 0.0,
    },
    "Ground": {
        "Fire": 2.0,
        "Electric": 2.0,
        "Poison": 2.0,
        "Rock": 2.0,
        "Steel": 2.0,
        "Grass": 0.5,
        "Bug": 0.5,
        "Flying": 0.0,
    },
    "Flying": {
        "Grass": 2.0,
        "Fighting": 2.0,
        "Bug": 2.0,
        "Electric": 0.5,
        "Rock": 0.5,
        "Steel": 0.5,
    },
    "Psychic": {
        "Fighting": 2.0,
        "Poison": 2.0,
        "Psychic": 0.5,
        "Steel": 0.5,
        "Dark": 0.5,
    },
    "Bug": {
        "Grass": 2.0,
        "Psychic": 2.0,
        "Dark": 2.0,
        "Fire": 0.5,
        "Fighting": 0.5,
        "Poison": 0.5,
        "Flying": 0.5,
        "Ghost": 0.5,
        "Steel": 0.5,
        "Fairy": 0.5,
    },
    "Rock": {
        "Fire": 2.0,
        "Ice": 2.0,
        "Flying": 2.0,
        "Bug": 2.0,
        "Fighting": 0.5,
        "Ground": 0.5,
        "Steel": 0.5,
    },
    "Ghost": {
        "Psychic": 2.0,
        "Ghost": 2.0,
        "Dark": 2.0,
        "Normal": 0.0,
        "Steel": 0.5,
    },
    "Dragon": {
        "Dragon": 2.0,
        "Steel": 0.5,
        "Fairy": 0.5,
    },
    "Dark": {
        "Psychic": 2.0,
        "Ghost": 2.0,
        "Fighting": 0.5,
        "Dark": 0.5,
        "Fairy": 0.5,
    },
    "Steel": {
        "Ice": 2.0,
        "Rock": 2.0,
        "Fairy": 2.0,
        "Fire": 0.5,
        "Water": 0.5,
        "Electric": 0.5,
        "Steel": 0.5,
    },
    "Fairy": {
        "Fighting": 2.0,
        "Dragon": 2.0,
        "Dark": 2.0,
        "Fire": 0.5,
        "Poison": 0.5,
        "Steel": 0.5,
    },
}

# Discord ```ansi``` (green = advantage, red = weak, grey = neutral)
_RST = "\u001b[0m"
_GREEN = "\u001b[0;32m"
_RED = "\u001b[0;31m"
_GREY = "\u001b[0;90m"


def normalize_type_key(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return "Normal"
    if s in _RAW_TO_CHART:
        return _RAW_TO_CHART[s]
    if s in ALL_TYPES:
        return s
    return "Normal"


def tcg_display_name(chart_key: str) -> str:
    return _CHART_TO_TCG_DISPLAY.get(chart_key, chart_key)


def defense_types_from_card_types(raw: object, *, supertype: str | None) -> tuple[str, ...]:
    """Chart keys for the defending Pokémon (empty / unknown → Normal)."""
    st = (supertype or "").strip()
    if st and st not in ("Pokémon", "Pokemon"):
        return ("Normal",)
    if not isinstance(raw, list) or not raw:
        return ("Normal",)
    seen: list[str] = []
    for item in raw:
        k = normalize_type_key(str(item))
        if k not in seen:
            seen.append(k)
    return tuple(seen) if seen else ("Normal",)


def move_attacking_chart_type(cost: Sequence[str] | None) -> str:
    """Use the first non-Colorless energy in the attack cost; else Normal (Colorless)."""
    if not cost:
        return "Normal"
    for raw in cost:
        k = normalize_type_key(str(raw))
        if k != "Normal":
            return k
    return "Normal"


def combined_type_factor(attacking: str, defending_types: Sequence[str]) -> float:
    if not defending_types:
        return 1.0
    factor = 1.0
    row = _ATK_VS_DEF.get(attacking, {})
    for d in defending_types:
        factor *= float(row.get(d, 1.0))
    return factor


def scaled_damage(base_damage: int, type_factor: float) -> int:
    """Integer damage after type modifier (floor toward zero, not below 0)."""
    if base_damage <= 0:
        return 0
    if type_factor <= 0:
        return 0
    return max(0, int(math.floor(float(base_damage) * type_factor + 1e-9)))


def ansi_colored_battle_line(plain_line: str, *, type_factor: float) -> str:
    """Wrap a battle log segment for Discord ```ansi``` blocks."""
    if type_factor > 1.0:
        color = _GREEN
    elif type_factor < 1.0:
        color = _RED
    else:
        color = _GREY
    return f"{color}{plain_line}{_RST}"
