"""In-game shop SKUs mapped to Stripe Price IDs (from environment)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ShopCurrency = Literal["pokedollars", "crystals"]
ShopKind = Literal["currency", "drop_boost", "random_pack"]

HALF_DROP_COOLDOWN_SKU_ID = "half_drop_cooldown"
RANDOM_PACK_SKU_ID = "random_pack"


@dataclass(frozen=True)
class ShopSkuDef:
    id: str
    kind: ShopKind
    title: str
    description: str
    currency: ShopCurrency | None = None
    grant_amount: int = 0
    badge: str | None = None
    one_time: bool = False


SHOP_SKU_DEFS: tuple[ShopSkuDef, ...] = (
    ShopSkuDef(
        id="pokedollars_2500",
        kind="currency",
        currency="pokedollars",
        grant_amount=2500,
        title="Starter stash",
        description="₽2,500 Pokedollars for packs, auctions, and duels.",
    ),
    ShopSkuDef(
        id="pokedollars_10000",
        kind="currency",
        currency="pokedollars",
        grant_amount=10_000,
        title="Trainer bundle",
        description="₽10,000 Pokedollars — popular mid-tier top-up.",
        badge="popular",
    ),
    ShopSkuDef(
        id="pokedollars_50000",
        kind="currency",
        currency="pokedollars",
        grant_amount=50_000,
        title="Champion vault",
        description="₽50,000 Pokedollars for serious collectors.",
        badge="best_value",
    ),
    ShopSkuDef(
        id="crystals_15",
        kind="currency",
        currency="crystals",
        grant_amount=15,
        title="Crystal pouch",
        description="15 Crystals for premium packs and grading.",
    ),
    ShopSkuDef(
        id="crystals_50",
        kind="currency",
        currency="crystals",
        grant_amount=50,
        title="Crystal satchel",
        description="50 Crystals — great for repeated pack opens.",
        badge="popular",
    ),
    ShopSkuDef(
        id="crystals_100",
        kind="currency",
        currency="crystals",
        grant_amount=100,
        title="Crystal hoard",
        description="100 Crystals for long-term grinding.",
        badge="best_value",
    ),
    ShopSkuDef(
        id=HALF_DROP_COOLDOWN_SKU_ID,
        kind="drop_boost",
        title="Half drop cooldown",
        description=(
            "One-time purchase: your /cd and /cs card-drop wait is permanently "
            "cut in half."
        ),
        one_time=True,
    ),
    ShopSkuDef(
        id=RANDOM_PACK_SKU_ID,
        kind="random_pack",
        title="Random booster pack",
        description=(
            "Grants one random active-series pack in your inventory. Open with /packv."
        ),
    ),
)

SKU_ENV_KEYS: dict[str, str] = {
    "pokedollars_2500": "STRIPE_PRICE_POKEDOLLARS_2500",
    "pokedollars_10000": "STRIPE_PRICE_POKEDOLLARS_10000",
    "pokedollars_50000": "STRIPE_PRICE_POKEDOLLARS_50000",
    "crystals_15": "STRIPE_PRICE_CRYSTALS_15",
    "crystals_50": "STRIPE_PRICE_CRYSTALS_50",
    "crystals_100": "STRIPE_PRICE_CRYSTALS_100",
    HALF_DROP_COOLDOWN_SKU_ID: "STRIPE_PRICE_HALF_DROP_COOLDOWN",
    RANDOM_PACK_SKU_ID: "STRIPE_PRICE_RANDOM_PACK",
}


def sku_by_id(sku_id: str) -> ShopSkuDef | None:
    for sku in SHOP_SKU_DEFS:
        if sku.id == sku_id:
            return sku
    return None


def currency_skus() -> tuple[ShopSkuDef, ...]:
    return tuple(s for s in SHOP_SKU_DEFS if s.kind == "currency")


def perk_skus() -> tuple[ShopSkuDef, ...]:
    return tuple(s for s in SHOP_SKU_DEFS if s.kind in ("drop_boost", "random_pack"))
