"""Map Pokémon TCG API printed `rarity` strings onto normalized tier codes."""

from __future__ import annotations

# Codes must match seeded rows in `rarity_classes` (see Alembic migration).
VALID_CODES = frozenset(
    {
        "common",
        "uncommon",
        "rare",
        "rare_holo",
        "ultra_rare",
        "double_rare",
        "illustration_rare",
        "special_rare",
        "hyper_rare",
        "chase",
    }
)

# Exact strings seen in API (lowercased keys).
_EXACT: dict[str, str] = {
    "common": "common",
    "uncommon": "uncommon",
    "rare": "rare",
    "rare holo": "rare_holo",
    "rare holo ex": "ultra_rare",
    "rare holo gx": "ultra_rare",
    "rare holo v": "ultra_rare",
    "rare holo vmax": "ultra_rare",
    "rare holo vstar": "ultra_rare",
    "rare ultra": "ultra_rare",
    "double rare": "double_rare",
    "ultra rare": "ultra_rare",
    "illustration rare": "illustration_rare",
    "special illustration rare": "special_rare",
    "hyper rare": "hyper_rare",
    "shiny rare": "illustration_rare",
    "shiny ultra rare": "ultra_rare",
    "amazing rare": "special_rare",
    "radiant rare": "double_rare",
    "promo": "rare",
    "classic collection": "special_rare",
    "ace spec rare": "ultra_rare",
    "none": "common",
}


def normalize_tcg_rarity(raw: str | None) -> str:
    """Return a `rarity_classes.code` for the given printed rarity label."""
    if raw is None or not str(raw).strip():
        return "common"

    key = str(raw).strip().lower()
    if key in _EXACT:
        return _EXACT[key]

    lowered = key
    # Keyword heuristics for variants not in _EXACT.
    if "chase" in lowered or "gold" in lowered or "rainbow" in lowered:
        return "chase"
    if "hyper" in lowered:
        return "hyper_rare"
    if "special illustration" in lowered or "special art" in lowered:
        return "special_rare"
    if "illustration" in lowered:
        return "illustration_rare"
    if "double" in lowered:
        return "double_rare"
    if "ultra" in lowered or " gx" in lowered or " ex" in lowered or " vmax" in lowered:
        return "ultra_rare"
    if "holo" in lowered:
        return "rare_holo"
    if "rare" in lowered:
        return "rare"
    if "uncommon" in lowered:
        return "uncommon"
    if "common" in lowered:
        return "common"

    # Safe default — uncommon sits in the middle of the curve.
    return "uncommon"


def assert_valid_code(code: str) -> str:
    if code not in VALID_CODES:
        msg = f"Unknown rarity code {code!r}; expected one of {sorted(VALID_CODES)}"
        raise ValueError(msg)
    return code
