"""Per-card pull odds for a booster series (matches :mod:`poke_pon_bot.services.drops`)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_series import CardSeries
from poke_pon_bot.models.drops import DropTable, DropWeight
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.drops import (
    CODE_SLOT_LUCK_MULTIPLIER,
    PACK_OPEN_LUCK_PERCENT,
    _CODE_SLOT_RARITY_WEIGHT_MULT,
    card_pull_weight,
    pack_price_luck_bonus,
    pack_slot_luck_percent,
)
from poke_pon_bot.services.excluded_sets import (
    excluded_set_clause,
    filter_excluded_set_codes,
)
from poke_pon_bot.services.rarity_luck import luck_rarity_weight_multiplier
from poke_pon_bot.services.collection_sell import quote_collection_sell_payout
from poke_pon_bot.services.packs import PackService


class _SellQuoteInstance:
    """Stand-in for shop sell quote on catalog printings (no owned copy)."""

    evolution_stages = 0
    grade = None


def _max_attack_damage(attacks: Any) -> int:
    if not isinstance(attacks, list):
        return 0
    best = 0
    for atk in attacks:
        if not isinstance(atk, dict):
            continue
        raw = atk.get("damage")
        if raw is None:
            continue
        digits: list[str] = []
        for ch in str(raw):
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            best = max(best, int("".join(digits)))
    return best


def _effective_weights(
    base: list[tuple[int, float]],
    *,
    eligible_ids: set[int],
    code_slot: bool,
    luck_percent: float,
) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for rid, w in base:
        if int(rid) not in eligible_ids:
            continue
        mult = 1.0
        if code_slot:
            mult *= float(_CODE_SLOT_RARITY_WEIGHT_MULT.get(int(rid), 1.0))
        if luck_percent != 0:
            mult *= luck_rarity_weight_multiplier(int(rid), luck_percent)
        out.append((int(rid), float(w) * mult))
    return out


def _tier_percents(weights: list[tuple[int, float]]) -> dict[int, float]:
    total = sum(w for _, w in weights)
    if total <= 0:
        return {}
    return {int(rid): (float(w) / total) * 100.0 for rid, w in weights}


def _per_pack_any_slot_percent(per_card_percent: float, slot_count: int) -> float:
    """Chance at least one copy of a card appears across independent slots."""
    if slot_count <= 0 or per_card_percent <= 0:
        return 0.0
    p = float(per_card_percent) / 100.0
    return (1.0 - (1.0 - p) ** int(slot_count)) * 100.0


async def build_series_pull_odds(
    session: AsyncSession,
    series: CardSeries,
    *,
    luck_percent: float = PACK_OPEN_LUCK_PERCENT,
) -> dict[str, Any]:
    """Odds for one random slot (regular vs code) in this series's pool."""
    ps = PackService()
    raw_codes = await ps._series_set_codes(session, series.id)  # noqa: SLF001
    set_codes = sorted(filter_excluded_set_codes(raw_codes))
    if not set_codes:
        return {
            "series": _series_meta(series, set_codes),
            "tiers": {"regular": [], "code": []},
            "cards": [],
            "notes": ["This series has no linked TCG sets in the catalog."],
        }

    drop_table = await session.scalar(
        select(DropTable).where(DropTable.code == "default")
    )
    if drop_table is None:
        raise LookupError("Default drop table missing.")

    weight_rows = await session.execute(
        select(DropWeight.rarity_class_id, DropWeight.weight).where(
            DropWeight.drop_table_id == drop_table.id
        )
    )
    base_weights = [(int(r), float(w)) for r, w in weight_rows.all()]
    if not base_weights:
        raise LookupError("Drop table has no weights.")

    cards_stmt = (
        select(Card, RarityClass)
        .join(RarityClass, RarityClass.id == Card.rarity_class_id)
        .where(Card.set_code.in_(set_codes))
        .where(excluded_set_clause(Card.set_code))
        .order_by(RarityClass.sort_order.desc(), Card.name.asc())
    )
    rows = (await session.execute(cards_stmt)).all()

    by_tier: dict[int, list[Card]] = defaultdict(list)
    rarity_meta: dict[int, RarityClass] = {}
    for card, rarity in rows:
        by_tier[int(card.rarity_class_id)].append(card)
        rarity_meta[int(rarity.id)] = rarity

    tier_total_pull_weight: dict[int, float] = {}
    for rid, cards_in_tier in by_tier.items():
        tier_total_pull_weight[rid] = sum(card_pull_weight(c) for c in cards_in_tier) or 1.0

    eligible_ids = {rid for rid, lst in by_tier.items() if lst}
    main_luck = pack_slot_luck_percent(
        crystal_price=int(series.crystal_price),
        code_slot=False,
        base_luck_percent=luck_percent,
    )
    code_luck = pack_slot_luck_percent(
        crystal_price=int(series.crystal_price),
        code_slot=True,
        base_luck_percent=luck_percent,
    )
    reg_weights = _effective_weights(
        base_weights,
        eligible_ids=eligible_ids,
        code_slot=False,
        luck_percent=main_luck,
    )
    code_weights = _effective_weights(
        base_weights,
        eligible_ids=eligible_ids,
        code_slot=True,
        luck_percent=code_luck,
    )
    reg_tier_pct = _tier_percents(reg_weights)
    code_tier_pct = _tier_percents(code_weights)

    def _tier_rows(tier_pct: dict[int, float]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rid in sorted(tier_pct.keys(), key=lambda x: rarity_meta[x].sort_order):
            r = rarity_meta[rid]
            out.append(
                {
                    "rarity_class_id": rid,
                    "code": r.code,
                    "display_name": r.display_name,
                    "sort_order": int(r.sort_order),
                    "card_count": len(by_tier.get(rid, [])),
                    "tier_chance_percent": round(tier_pct[rid], 4),
                }
            )
        return sorted(out, key=lambda t: t["sort_order"], reverse=True)

    main_slots = int(series.cards_per_pack)
    code_slots = int(series.code_cards_per_pack)

    card_rows: list[dict[str, Any]] = []
    for card, rarity in rows:
        rid = int(card.rarity_class_id)
        reg_tier = reg_tier_pct.get(rid, 0.0)
        code_tier = code_tier_pct.get(rid, 0.0)
        w = card_pull_weight(card)
        tw = tier_total_pull_weight.get(rid, 1.0)
        card_share = w / tw
        reg_card = reg_tier * card_share
        code_card = code_tier * card_share
        reg_pack = _per_pack_any_slot_percent(reg_card, main_slots)
        code_pack = _per_pack_any_slot_percent(code_card, code_slots)
        card_rows.append(
            {
                "card_id": int(card.id),
                "name": card.name,
                "set_code": card.set_code,
                "set_name": card.set_name,
                "collector_number": card.collector_number,
                "image_small_url": card.image_small_url,
                "image_large_url": card.image_large_url,
                "hp": card.hp,
                "supertype": card.supertype,
                "types": list(card.tcg_types or []),
                "attacks": card.attacks if isinstance(card.attacks, list) else [],
                "max_damage": _max_attack_damage(card.attacks),
                "tcg_rarity": card.tcg_rarity,
                "shop_sell_pokedollars": quote_collection_sell_payout(
                    card, rarity, _SellQuoteInstance()
                ),
                "rarity": {
                    "code": rarity.code,
                    "display_name": rarity.display_name,
                    "sort_order": int(rarity.sort_order),
                },
                "regular": {
                    "tier_chance_percent": round(reg_tier, 4),
                    "per_card_chance_percent": round(reg_card, 4),
                    "per_pack_chance_percent": round(reg_pack, 4),
                },
                "code_slot": {
                    "tier_chance_percent": round(code_tier, 4),
                    "per_card_chance_percent": round(code_card, 4),
                    "per_pack_chance_percent": round(code_pack, 4),
                },
                "combined_per_pack_chance_percent": round(
                    100.0
                    * (
                        1.0
                        - (1.0 - reg_pack / 100.0) * (1.0 - code_pack / 100.0)
                    ),
                    4,
                ),
            }
        )
    card_rows.sort(
        key=lambda c: float(c.get("combined_per_pack_chance_percent") or 0),
        reverse=True,
    )

    price_bonus = pack_price_luck_bonus(int(series.crystal_price))
    notes = [
        f"Each of the {main_slots} main slots rolls independently (same odds).",
        f"The code card slot ({code_slots} per pack) uses a rarer tier mix and "
        f"**{CODE_SLOT_LUCK_MULTIPLIER:g}×** the pack luck of main slots.",
        "Within a tier, cards are weighted by printed TCG rarity, HP, and max attack damage — stronger/rarer cards pull less often.",
        f"Pack opens include +{luck_percent:g}% base luck"
        + (f" and +{price_bonus:g}% for this pack's crystal price" if price_bonus else "")
        + f" (main slots ≈ +{main_luck:g}%; code slot ≈ +{code_luck:g}%; server boosts may add more).",
        "Per-pack % is the chance that card appears at least once when opening one booster.",
        "Duplicate avoidance in a single pack is not reflected in these numbers.",
    ]

    return {
        "series": _series_meta(series, set_codes),
        "tiers": {"regular": _tier_rows(reg_tier_pct), "code": _tier_rows(code_tier_pct)},
        "cards": card_rows,
        "notes": notes,
    }


def _series_meta(series: CardSeries, set_codes: list[str]) -> dict[str, Any]:
    return {
        "code": series.code,
        "display_name": series.display_name,
        "description": series.description,
        "crystal_price": int(series.crystal_price),
        "pack_art_url": series.pack_art_url,
        "cards_per_pack": int(series.cards_per_pack),
        "code_cards_per_pack": int(series.code_cards_per_pack),
        "set_codes": list(set_codes),
    }


def _matches_pack_search(row: dict[str, Any], q: str) -> bool:
    if not q:
        return True
    pack = row.get("pack") or {}
    card = row.get("card") or {}
    hay = " ".join(
        [
            str(pack.get("display_name") or ""),
            str(pack.get("code") or ""),
            str(card.get("name") or ""),
            str(card.get("set_code") or ""),
            str(card.get("set_name") or ""),
        ]
    ).lower()
    return q in hay


def _sort_pack_search_rows(rows: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    key = (sort or "top").strip().lower()

    def _top(r: dict[str, Any]) -> float:
        card = r.get("card") or {}
        return float(card.get("combined_per_pack_chance_percent") or 0)

    def _rarity(r: dict[str, Any]) -> int:
        card = r.get("card") or {}
        rarity = card.get("rarity") or {}
        return int(rarity.get("sort_order") or 0)

    def _sell(r: dict[str, Any]) -> int:
        card = r.get("card") or {}
        return int(card.get("shop_sell_pokedollars") or 0)

    def _pack_cost(r: dict[str, Any]) -> int:
        pack = r.get("pack") or {}
        return int(pack.get("crystal_price") or 0)

    if key == "rarity":
        return sorted(
            rows,
            key=lambda r: (-_rarity(r), (r.get("card") or {}).get("name") or ""),
        )
    if key == "cost_high":
        return sorted(
            rows,
            key=lambda r: (-_sell(r), -_pack_cost(r), (r.get("card") or {}).get("name") or ""),
        )
    if key == "cost_low":
        return sorted(
            rows,
            key=lambda r: (_sell(r), _pack_cost(r), (r.get("card") or {}).get("name") or ""),
        )
    return sorted(rows, key=lambda r: (-_top(r), (r.get("card") or {}).get("name") or ""))


async def search_global_pack_pool(
    session: AsyncSession,
    *,
    q: str = "",
    sort: str = "top",
    pack_code: str = "",
    page: int = 1,
    page_size: int = 60,
    luck_percent: float = PACK_OPEN_LUCK_PERCENT,
) -> dict[str, Any]:
    """Flatten all active booster pools for global search (card + pack metadata)."""
    ps = PackService()
    series_list = await ps.list_active_series(session)
    pack_filter = (pack_code or "").strip().lower()
    if pack_filter:
        series_list = [s for s in series_list if (s.code or "").lower() == pack_filter]
    needle = (q or "").strip().lower()
    rows: list[dict[str, Any]] = []

    for series in series_list:
        payload = await build_series_pull_odds(session, series, luck_percent=luck_percent)
        pack_meta = {
            "code": payload["series"]["code"],
            "display_name": payload["series"]["display_name"],
            "crystal_price": payload["series"]["crystal_price"],
            "pack_art_url": payload["series"].get("pack_art_url"),
        }
        for card in payload.get("cards") or []:
            row = {"pack": pack_meta, "card": card}
            if _matches_pack_search(row, needle):
                rows.append(row)

    rows = _sort_pack_search_rows(rows, sort)
    total = len(rows)
    page = max(1, int(page))
    page_size = min(120, max(1, int(page_size)))
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "sort": (sort or "top").strip().lower() or "top",
        "items": rows[start : start + page_size],
    }
