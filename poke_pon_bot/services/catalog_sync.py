"""Import Pokémon TCG API cards into the local catalog."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.rarity import RarityClass, TcgRarityMapping
from poke_pon_bot.services.rarity_normalize import normalize_tcg_rarity

LOG = logging.getLogger(__name__)

TCG_BASE = "https://api.pokemontcg.io/v2"


def load_set_ids_from_yaml(path: Path) -> list[str]:
    """Resolve YAML path relative to cwd if not absolute."""
    p = path if path.is_absolute() else Path.cwd() / path
    if not p.is_file():
        raise FileNotFoundError(f"CARD_SETS_CONFIG not found: {p}")

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw = data.get("sets") or []
    if not isinstance(raw, list) or not raw:
        raise ValueError("YAML must contain a non-empty `sets:` list of TCG set IDs")
    return [str(x).strip() for x in raw if str(x).strip()]


async def _rarity_lookup(session: AsyncSession) -> tuple[dict[str, int], dict[str, int]]:
    """Returns (code -> id) and (exact tcg rarity string lower -> id overrides)."""
    rc_rows = await session.execute(select(RarityClass))
    codes = {rc.code: rc.id for rc in rc_rows.scalars()}

    map_rows = await session.execute(select(TcgRarityMapping))
    overrides = {
        m.tcg_rarity.strip().lower(): m.rarity_class_id for m in map_rows.scalars()
    }

    return codes, overrides


def _card_row_dict(
    payload: dict[str, Any],
    rarity_class_id: int,
) -> dict[str, Any]:
    images = payload.get("images") or {}
    small = images.get("small") or ""
    large = images.get("large") or small
    tcg_set = payload.get("set") or {}
    dex_raw = payload.get("nationalPokedexNumbers")
    dex_numbers: list[int] | None
    if isinstance(dex_raw, list):
        dex_numbers = []
        for x in dex_raw:
            if isinstance(x, bool):
                continue
            if isinstance(x, int):
                dex_numbers.append(x)
            elif isinstance(x, float):
                dex_numbers.append(int(x))
    else:
        dex_numbers = None

    hp_val = payload.get("hp")
    hp_str = str(hp_val) if hp_val is not None else None

    return {
        "tcg_card_id": payload["id"],
        "name": payload.get("name") or "Unknown",
        "set_code": tcg_set.get("id") or "",
        "set_name": tcg_set.get("name") or "",
        "collector_number": str(payload.get("number") or ""),
        "tcg_rarity": payload.get("rarity"),
        "image_small_url": small,
        "image_large_url": large,
        "supertype": payload.get("supertype"),
        "hp": hp_str,
        "dex_numbers": dex_numbers,
        "rarity_class_id": rarity_class_id,
    }


async def sync_curated_sets(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    set_ids: list[str],
    api_key: str | None = None,
) -> dict[str, int]:
    """Fetch all cards for each set ID and upsert into `cards`. Returns per-set counts."""

    headers: dict[str, str] = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    counts: dict[str, int] = {sid: 0 for sid in set_ids}

    async with session_factory() as session:
        codes_by_class, rarity_overrides = await _rarity_lookup(session)

        async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
            for set_id in set_ids:
                page = 1
                page_size = 250

                while True:
                    params = {
                        "q": f"set.id:{set_id}",
                        "page": page,
                        "pageSize": page_size,
                    }
                    resp = await client.get(f"{TCG_BASE}/cards", params=params)
                    resp.raise_for_status()
                    body = resp.json()
                    data = body.get("data") or []
                    total_count = int(body.get("totalCount") or 0)

                    for payload in data:
                        raw_rarity = payload.get("rarity")
                        key = (raw_rarity or "").strip().lower()
                        if key in rarity_overrides:
                            rid = rarity_overrides[key]
                        else:
                            code = normalize_tcg_rarity(raw_rarity)
                            rid = codes_by_class.get(code)
                            if rid is None:
                                rid = codes_by_class["uncommon"]

                        values = _card_row_dict(payload, rid)
                        existing = await session.scalar(
                            select(Card).where(Card.tcg_card_id == values["tcg_card_id"])
                        )
                        if existing:
                            for k, v in values.items():
                                setattr(existing, k, v)
                        else:
                            session.add(Card(**values))

                        counts[set_id] += 1

                    await session.commit()

                    if page * page_size >= total_count or not data:
                        LOG.info(
                            "Finished set %s — stored %s cards (API reports %s)",
                            set_id,
                            counts[set_id],
                            total_count,
                        )
                        break
                    page += 1

    return counts
