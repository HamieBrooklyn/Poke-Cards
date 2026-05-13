"""Series sync at boot — combines two layers:

1. ``upsert_pack_series`` — applies hand-curated overrides from ``config/pack_series.v1.yaml``.
2. ``sync_pack_series_from_catalog`` — for every imported TCG set in ``cards``, ensures a
   series exists with pack art: **Scrydex** front-of-booster PNG when available
   (``images.scrydex.com``), otherwise the official ``pokemontcg.io`` set logo. Probe
   results are cached in ``data/.pack_art_url_cache.json`` so restarts stay fast.

Together they keep ``card_series`` aligned with reality: YAML wins for codes it lists, every
real set gets an auto-discovered series for free, and stale rows that match neither layer
get pruned by ``prune_orphan_series``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_series import CardSeries, CardSeriesSet
from poke_pon_bot.models.pack_instance import UserPackInstance
from poke_pon_bot.services.excluded_sets import is_excluded_set_code


def pokemontcg_logo_url(set_code: str) -> str:
    """Public CDN URL pattern documented on https://docs.pokemontcg.io/."""
    return f"https://images.pokemontcg.io/{set_code}/logo.png"


def scrydex_booster_pack_image_url(set_code: str) -> str:
    """Front-of-pack art on Scrydex's public image CDN (see https://scrydex.com/docs/pokemon/sealed)."""
    return f"https://images.scrydex.com/pokemon/{set_code}-s1/large"


# Scrydex serves this exact byte length for a generic placeholder when no real booster exists.
_PLACEHOLDER_IMAGE_BYTES = 186316

_PACK_ART_CACHE_PATH = Path("data/.pack_art_url_cache.json")

# Crystal price tiers keyed off the highest ``rarity_classes.id`` (1=common … 10=chase) found in
# the cards backing the series. Curve picked so an everyday "rare-only" set stays affordable
# while a chase-tier set commands ~5x the cost of the random ``/packd`` Crystal price.
_DEFAULT_CRYSTAL_PRICE = 5
_CRYSTAL_PRICE_BY_TOP_RARITY: dict[int, int] = {
    1: 5,   # common
    2: 5,   # uncommon
    3: 5,   # rare
    4: 6,   # rare_holo
    5: 8,   # ultra_rare
    6: 10,  # double_rare
    7: 13,  # illustration_rare
    8: 16,  # special_rare
    9: 20,  # hyper_rare
    10: 25, # chase
}


def crystal_price_for_top_rarity(top_rarity_class_id: int | None) -> int:
    """Public mapping helper — also used by ``/packv`` description if needed."""
    if top_rarity_class_id is None:
        return _DEFAULT_CRYSTAL_PRICE
    return _CRYSTAL_PRICE_BY_TOP_RARITY.get(int(top_rarity_class_id), _DEFAULT_CRYSTAL_PRICE)


async def _max_rarity_by_set_code(session: AsyncSession) -> dict[str, int]:
    """Return ``{set_code: max(rarity_class_id)}`` so series prices can be derived in one pass."""
    rows = await session.execute(
        select(Card.set_code, func.max(Card.rarity_class_id))
        .where(~func.lower(Card.set_code).like("mcd%"))
        .group_by(Card.set_code)
    )
    out: dict[str, int] = {}
    for set_code, top in rows.all():
        sc = (set_code or "").strip()
        if not sc or top is None:
            continue
        out[sc] = int(top)
    return out

_LOG = logging.getLogger(__name__)

DEFAULT_PACK_SERIES_CONFIG = Path("config/pack_series.v1.yaml")


def _read_pack_art_cache() -> dict[str, str]:
    try:
        raw = json.loads(_PACK_ART_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def _write_pack_art_cache(cache: dict[str, str]) -> None:
    try:
        _PACK_ART_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        sorted_pairs = {k: cache[k] for k in sorted(cache)}
        _PACK_ART_CACHE_PATH.write_text(
            json.dumps(sorted_pairs, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _LOG.warning("Could not persist pack art cache to %s: %s", _PACK_ART_CACHE_PATH, exc)


async def _probe_scrydex_or_logo(client: httpx.AsyncClient, set_code: str) -> str:
    """Return Scrydex pack art URL when it looks like a real booster; else the pokemontcg.io logo."""
    scrydex_url = scrydex_booster_pack_image_url(set_code)
    logo_url = pokemontcg_logo_url(set_code)
    try:
        head = await client.head(scrydex_url, follow_redirects=True)
    except httpx.RequestError as exc:
        _LOG.debug("Pack art HEAD failed for %s: %s — using logo", set_code, exc)
        return logo_url
    if head.status_code != 200:
        return logo_url
    cl_raw = head.headers.get("content-length")
    if cl_raw is not None:
        try:
            cl_val = int(str(cl_raw).strip())
        except ValueError:
            cl_val = None
        if cl_val == _PLACEHOLDER_IMAGE_BYTES:
            return logo_url
        if cl_val is not None and cl_val != _PLACEHOLDER_IMAGE_BYTES:
            return scrydex_url
    try:
        get = await client.get(scrydex_url, follow_redirects=True)
    except httpx.RequestError as exc:
        _LOG.debug("Pack art GET fallback failed for %s: %s — using logo", set_code, exc)
        return logo_url
    if get.status_code != 200:
        return logo_url
    if len(get.content) == _PLACEHOLDER_IMAGE_BYTES:
        return logo_url
    return scrydex_url


async def _resolve_pack_art_urls(set_codes: set[str]) -> dict[str, str]:
    """Map each set code to a final ``pack_art_url``, updating the on-disk cache for new keys."""
    cache = _read_pack_art_cache()
    missing = sorted(s for s in set_codes if s not in cache)
    if missing:
        sem = asyncio.Semaphore(10)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "PokePonBot/1.0 (pack series sync)"},
        ) as client:

            async def one(code: str) -> tuple[str, str]:
                async with sem:
                    url = await _probe_scrydex_or_logo(client, code)
                return code, url

            resolved_pairs = await asyncio.gather(*(one(c) for c in missing))
        for code, url in resolved_pairs:
            cache[code] = url
        _write_pack_art_cache(cache)

    return {c: cache[c] for c in set_codes}


def _resolve_path(path: Path | str | None) -> Path:
    p = Path(path) if path else DEFAULT_PACK_SERIES_CONFIG
    return p if p.is_absolute() else Path.cwd() / p


def load_pack_series_yaml(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Parse the YAML file and return a list of normalized series dicts."""
    p = _resolve_path(path)
    if not p.is_file():
        _LOG.info("Pack series config not found at %s — no series will be available.", p)
        return []

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top-level YAML must be a mapping.")

    series_raw = raw.get("series") or []
    if not isinstance(series_raw, list):
        raise ValueError(f"{p}: `series:` must be a list.")

    out: list[dict[str, Any]] = []
    for idx, entry in enumerate(series_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{p}: series[{idx}] must be a mapping.")
        code = str(entry.get("code") or "").strip()
        if not code:
            raise ValueError(f"{p}: series[{idx}] missing `code`.")
        display = str(entry.get("display_name") or code).strip()
        sets_raw = entry.get("sets") or []
        if not isinstance(sets_raw, list):
            raise ValueError(f"{p}: series[{idx}] `sets` must be a list.")
        sets = sorted({str(s).strip() for s in sets_raw if str(s).strip()})

        out.append(
            {
                "code": code,
                "display_name": display,
                "description": (entry.get("description") or None),
                "crystal_price": int(entry.get("crystal_price") or 5),
                "pack_art_url": (entry.get("pack_art_url") or None),
                "cards_per_pack": int(entry.get("cards_per_pack") or 10),
                "code_cards_per_pack": int(entry.get("code_cards_per_pack") or 1),
                "is_active": bool(entry.get("is_active", True)),
                "sets": sets,
            }
        )

    if len({s["code"] for s in out}) != len(out):
        raise ValueError(f"{p}: duplicate series `code` values.")
    return out


async def upsert_pack_series(
    session_factory: async_sessionmaker,
    *,
    config_path: Path | str | None = None,
) -> tuple[int, int]:
    """Reconcile ``card_series`` + ``card_series_sets`` from YAML.

    Returns a ``(series_upserted, sets_linked)`` tuple for logging.
    """
    payload = load_pack_series_yaml(config_path)
    if not payload:
        return (0, 0)

    series_count = 0
    sets_count = 0
    async with session_factory() as session:
        for entry in payload:
            existing = await session.scalar(
                select(CardSeries).where(CardSeries.code == entry["code"])
            )
            if existing is None:
                row = CardSeries(
                    code=entry["code"],
                    display_name=entry["display_name"],
                    description=entry["description"],
                    crystal_price=entry["crystal_price"],
                    pack_art_url=entry["pack_art_url"],
                    cards_per_pack=entry["cards_per_pack"],
                    code_cards_per_pack=entry["code_cards_per_pack"],
                    is_active=entry["is_active"],
                )
                session.add(row)
                await session.flush()
                series_id = row.id
            else:
                existing.display_name = entry["display_name"]
                existing.description = entry["description"]
                existing.crystal_price = entry["crystal_price"]
                existing.pack_art_url = entry["pack_art_url"]
                existing.cards_per_pack = entry["cards_per_pack"]
                existing.code_cards_per_pack = entry["code_cards_per_pack"]
                existing.is_active = entry["is_active"]
                series_id = existing.id
            series_count += 1

            current_rows = await session.execute(
                select(CardSeriesSet).where(CardSeriesSet.series_id == series_id)
            )
            current_sets = {r.set_code: r for r in current_rows.scalars()}
            wanted = set(entry["sets"])

            extras = [r.id for code, r in current_sets.items() if code not in wanted]
            if extras:
                await session.execute(
                    delete(CardSeriesSet).where(CardSeriesSet.id.in_(extras))
                )

            for code in wanted - current_sets.keys():
                session.add(CardSeriesSet(series_id=series_id, set_code=code))
                sets_count += 1

        await session.commit()

    _LOG.info(
        "Pack series synced: %s series, %s new set links from %s",
        series_count,
        sets_count,
        _resolve_path(config_path),
    )
    return (series_count, sets_count)


async def sync_pack_series_from_catalog(
    session_factory: async_sessionmaker,
    *,
    config_path: Path | str | None = None,
) -> int:
    """Ensure one ``card_series`` exists per distinct ``set_code`` in the catalog.

    Each auto-created series:
    - uses the TCG set id as ``code`` (e.g. ``sv1``),
    - copies ``set_name`` from any catalog row in that set as ``display_name``,
    - sets ``pack_art_url`` from the on-disk probe cache: real booster art from Scrydex when
      available, otherwise the pokemontcg.io set logo (see module docstring).

    Existing rows whose ``pack_art_url`` is still **null or the logo URL** are upgraded to
    the resolved art **unless** the series ``code`` is listed in ``pack_series.v1.yaml`` (YAML
    owns ``pack_art_url`` for those). Custom URLs are left untouched.

    Returns the number of newly created series.
    """
    yaml_codes = {entry["code"] for entry in load_pack_series_yaml(config_path)}
    created = 0
    upgraded_art = 0
    upgraded_price = 0
    async with session_factory() as session:
        rows = await session.execute(
            select(Card.set_code, Card.set_name).distinct()
        )
        catalog_sets: dict[str, str] = {}
        for set_code, set_name in rows.all():
            sc = (set_code or "").strip()
            if not sc or is_excluded_set_code(sc):
                continue
            catalog_sets.setdefault(sc, (set_name or sc).strip() or sc)

        if not catalog_sets:
            _LOG.info("Pack series auto-sync: no sets in catalog yet — skipping.")
            return 0

        pack_art_by_code = await _resolve_pack_art_urls(set(catalog_sets.keys()))
        max_rarity_by_set = await _max_rarity_by_set_code(session)

        existing = await session.execute(
            select(CardSeries).where(CardSeries.code.in_(catalog_sets.keys()))
        )
        by_code: dict[str, CardSeries] = {s.code: s for s in existing.scalars()}
        existing_codes = set(by_code.keys())

        for set_code, set_name in catalog_sets.items():
            pack_art_url = pack_art_by_code[set_code]
            top_rarity = max_rarity_by_set.get(set_code)
            crystal_price = crystal_price_for_top_rarity(top_rarity)
            if set_code not in existing_codes:
                row = CardSeries(
                    code=set_code,
                    display_name=set_name,
                    description=None,
                    crystal_price=crystal_price,
                    pack_art_url=pack_art_url,
                    cards_per_pack=10,
                    code_cards_per_pack=1,
                    is_active=True,
                )
                session.add(row)
                await session.flush()
                session.add(CardSeriesSet(series_id=row.id, set_code=set_code))
                created += 1
                continue

            if set_code in yaml_codes:
                continue
            series_row = by_code.get(set_code)
            if series_row is None:
                continue
            logo_url = pokemontcg_logo_url(set_code)
            cur = series_row.pack_art_url
            if cur in (None, logo_url) and pack_art_url != cur:
                series_row.pack_art_url = pack_art_url
                upgraded_art += 1
            # Price upgrade applies to catalog-managed rows only — YAML overrides win above.
            if series_row.crystal_price != crystal_price:
                series_row.crystal_price = crystal_price
                upgraded_price += 1

        if created or upgraded_art or upgraded_price:
            await session.commit()

    if created:
        _LOG.info("Pack series auto-sync: created %s series from catalog sets.", created)
    if upgraded_art:
        _LOG.info(
            "Pack series auto-sync: refreshed pack art for %s catalog series (logo → Scrydex or cache).",
            upgraded_art,
        )
    if upgraded_price:
        _LOG.info(
            "Pack series auto-sync: rebalanced crystal_price on %s catalog series (top-rarity tier).",
            upgraded_price,
        )
    return created


async def prune_orphan_series(
    session_factory: async_sessionmaker,
    *,
    config_path: Path | str | None = None,
) -> tuple[int, int]:
    """Reconcile ``card_series`` against the YAML + catalog ground-truth.

    For every series whose code is **not** in YAML and **not** an imported catalog set:
    - If no user pack instances reference it, DELETE the row (and its set links).
    - Otherwise hide it from ``/packv`` searches by flipping ``is_active`` off — keeping the row
      so the existing user packs still resolve their series cleanly.

    Returns ``(deleted_count, deactivated_count)``.
    """
    yaml_codes = {entry["code"] for entry in load_pack_series_yaml(config_path)}

    async with session_factory() as session:
        catalog_rows = await session.execute(select(Card.set_code).distinct())
        catalog_codes = {
            (c or "").strip()
            for c in catalog_rows.scalars()
            if (c or "").strip() and not is_excluded_set_code((c or "").strip())
        }

        keep = yaml_codes | catalog_codes
        all_series = await session.execute(select(CardSeries))
        orphans = [s for s in all_series.scalars() if s.code not in keep]
        if not orphans:
            return (0, 0)

        owned_ids = await session.execute(
            select(UserPackInstance.series_id).distinct()
        )
        protected = {int(x) for x in owned_ids.scalars()}

        deletable = [s for s in orphans if s.id not in protected]
        deactivate = [s for s in orphans if s.id in protected and s.is_active]

        if deletable:
            await session.execute(
                delete(CardSeries).where(CardSeries.id.in_([s.id for s in deletable]))
            )
        for s in deactivate:
            s.is_active = False

        if deletable or deactivate:
            await session.commit()

    if deletable:
        _LOG.info(
            "Pack series prune: deleted %s orphan series (codes: %s).",
            len(deletable),
            ", ".join(s.code for s in deletable),
        )
    if deactivate:
        _LOG.info(
            "Pack series prune: deactivated %s orphan series with existing packs (codes: %s).",
            len(deactivate),
            ", ".join(s.code for s in deactivate),
        )
    return (len(deletable), len(deactivate))
