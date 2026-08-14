"""Import Pokémon TCG API cards into the local catalog."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.rarity import RarityClass, TcgRarityMapping
from poke_pon_bot.services.excluded_sets import (
    filter_excluded_set_codes,
    is_excluded_set_code,
)
from poke_pon_bot.services.rarity_normalize import normalize_tcg_rarity

LOG = logging.getLogger(__name__)

TCG_BASE = "https://api.pokemontcg.io/v2"


@dataclass
class TcgSetMeta:
    set_id: str
    name: str
    release_date: str | None


@dataclass
class SetSyncStats:
    set_code: str
    set_name: str
    new_cards: int = 0
    updated_cards: int = 0
    is_new_set: bool = False
    new_card_ids: list[int] = field(default_factory=list)
    sample_cards: list[dict[str, Any]] = field(default_factory=list)

    def note_new_card(
        self,
        *,
        card_id: int,
        name: str,
        image_small_url: str | None,
        rarity_sort_order: int,
    ) -> None:
        self.new_card_ids.append(int(card_id))
        entry = {
            "id": int(card_id),
            "name": name,
            "image_small_url": image_small_url or "",
            "rarity_sort_order": int(rarity_sort_order),
        }
        self.sample_cards.append(entry)
        self.sample_cards.sort(
            key=lambda c: int(c.get("rarity_sort_order") or 0),
            reverse=True,
        )
        del self.sample_cards[5:]  # keep top 5 for Discord/website previews


@dataclass
class CatalogSyncResult:
    counts: dict[str, int]
    deltas: dict[str, SetSyncStats]


def _collector_sort_key(collector_number: str) -> tuple:
    """Order cards within a set (numeric when possible, else string)."""
    s = (collector_number or "").strip()
    if s.isdigit():
        return (0, int(s))
    return (1, s)


def _is_collector_after(pre_number: str, target_number: str) -> bool:
    return _collector_sort_key(target_number) > _collector_sort_key(pre_number)


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


def load_catalog_sync_plan_from_yaml(path: Path) -> dict[str, Any]:
    """Load sync plan from YAML.

    Supported keys (any combination may appear; they run in order):
    - `all_sets: true` — discover every non-excluded set from the Pokémon TCG
      ``/sets`` endpoint and import all of their cards. McDonald's promo sets are
      skipped because they duplicate other cards/art.
    - `query`: str — Pokémon TCG API `q=` expression (e.g. national dex range)
    - `sets`: list[str] — curated TCG set IDs to import in addition to the above.
    """
    p = path if path.is_absolute() else Path.cwd() / path
    if not p.is_file():
        raise FileNotFoundError(f"CARD_SETS_CONFIG not found: {p}")

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("YAML must be a mapping/object at the top level.")

    out: dict[str, Any] = {}
    if bool(data.get("all_sets")):
        out["all_sets"] = True

    raw_query = data.get("query")
    if isinstance(raw_query, str) and raw_query.strip():
        out["query"] = raw_query.strip()

    raw_sets = data.get("sets")
    if raw_sets is not None:
        if not isinstance(raw_sets, list):
            raise ValueError("`sets` must be a list of TCG set IDs.")
        sets = [str(x).strip() for x in raw_sets if str(x).strip()]
        if sets:
            out["sets"] = sets

    if not out:
        raise ValueError(
            "YAML must contain at least one of `all_sets: true`, a non-empty "
            "`sets:` list, or a `query:` string."
        )
    return out


async def fetch_all_set_ids(api_key: str | None = None) -> list[str]:
    """Hit ``/v2/sets`` and return every TCG set id known to pokemontcg.io.

    The API caps responses at 250 sets per page — we paginate until exhausted so
    even a future expansion past today's ~172 sets keeps working without code
    changes. Returned ids are sorted newest-first by release date when present so
    a partial sync still grabs the most recent expansions.
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
        page = 1
        page_size = 250
        while True:
            resp = await client.get(
                f"{TCG_BASE}/sets",
                params={"page": page, "pageSize": page_size},
            )
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data") or []
            rows.extend(d for d in data if isinstance(d, dict))
            total = int(body.get("totalCount") or 0)
            if page * page_size >= total or not data:
                break
            page += 1

    rows.sort(key=lambda d: str(d.get("releaseDate") or ""), reverse=True)
    return [
        str(d["id"])
        for d in rows
        if isinstance(d.get("id"), str)
        and not is_excluded_set_code(str(d["id"]))
    ]


async def fetch_sets_released_since(
    *,
    api_key: str | None = None,
    days: int = 60,
) -> list[TcgSetMeta]:
    """TCG sets with ``releaseDate`` within the last ``days`` (newest first)."""
    headers: dict[str, str] = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    cutoff = (datetime.now(UTC).date() - timedelta(days=max(1, int(days))))
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
        page = 1
        page_size = 250
        while True:
            resp = await client.get(
                f"{TCG_BASE}/sets",
                params={"page": page, "pageSize": page_size},
            )
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data") or []
            rows.extend(d for d in data if isinstance(d, dict))
            total = int(body.get("totalCount") or 0)
            if page * page_size >= total or not data:
                break
            page += 1

    out: list[TcgSetMeta] = []
    for d in rows:
        sid = str(d.get("id") or "").strip()
        if not sid or is_excluded_set_code(sid):
            continue
        release_raw = str(d.get("releaseDate") or "").strip()
        release_day = _parse_tcg_release_date(release_raw)
        # Skip undated sets — otherwise lookback is useless and we sync noise.
        if release_day is None or release_day < cutoff:
            continue
        out.append(
            TcgSetMeta(
                set_id=sid,
                name=str(d.get("name") or sid),
                release_date=release_raw or None,
            )
        )

    out.sort(
        key=lambda s: _parse_tcg_release_date(s.release_date) or date.min,
        reverse=True,
    )
    return out


def _parse_tcg_release_date(raw: str | None) -> date | None:
    """Pokémon TCG API uses ``YYYY/MM/DD``; also accept ISO ``YYYY-MM-DD``."""
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


async def _rarity_lookup(session: AsyncSession) -> tuple[dict[str, int], dict[str, int]]:
    """Returns (code -> id) and (exact tcg rarity string lower -> id overrides)."""
    rc_rows = await session.execute(select(RarityClass))
    codes = {rc.code: rc.id for rc in rc_rows.scalars()}

    map_rows = await session.execute(select(TcgRarityMapping))
    overrides = {
        m.tcg_rarity.strip().lower(): m.rarity_class_id for m in map_rows.scalars()
    }

    return codes, overrides


async def _resolve_evolves_for_set(
    session: AsyncSession,
    set_code: str,
) -> None:
    """Resolve ``evolves_to_card_id`` from API ``evolvesTo`` and backfill from ``evolvesFrom``.

    The API often omits ``evolvesTo`` on Basics but still sets ``evolvesFrom`` on the Stage card
    (e.g. Riolu has no ``evolvesTo`` while Lucario has ``evolvesFrom: "Riolu"``). We only link
    within the same set. When several Stage cards list the same ``evolvesFrom`` (e.g. two Lucario
    in one set), we pair in collector order and each target is used at most once. If there is
    only one such Stage in the set, every matching Basic in range may point to that same
    ``Card`` (multiple Riolu printings → one Lucario printing).
    """
    res = await session.execute(select(Card).where(Card.set_code == set_code))
    all_cards: list[Card] = list(res.scalars().all())

    for card in all_cards:
        names = card.evolves_to_names
        if not isinstance(names, list) or not names:
            card.evolves_to_card_id = None
            continue
        linked: int | None = None
        for tname in names:
            if not isinstance(tname, str) or not tname.strip():
                continue
            target = await session.scalar(
                select(Card)
                .where(
                    Card.set_code == card.set_code,
                    Card.name == tname.strip(),
                )
                .order_by(Card.collector_number.asc())
                .limit(1)
            )
            if target is not None and target.id != card.id:
                linked = target.id
                break
        card.evolves_to_card_id = linked

    used_target_ids: set[int] = {
        c.evolves_to_card_id
        for c in all_cards
        if c.evolves_to_card_id is not None
    }
    has_stage: list[Card] = [
        c
        for c in all_cards
        if c.evolves_from
        and str(c.evolves_from).strip()
    ]
    n_stage_by_prey: Counter[str] = Counter()
    for t in has_stage:
        n_stage_by_prey[(t.evolves_from or "").strip().casefold()] += 1

    for pre in sorted(
        all_cards,
        key=lambda c: _collector_sort_key(c.collector_number),
    ):
        if pre.evolves_to_card_id is not None:
            continue
        p_from = (pre.name or "").strip()
        if not p_from:
            continue
        prey_key = p_from.casefold()
        candidates: list[Card] = []
        for t in has_stage:
            if t.id == pre.id:
                continue
            t_ef = (t.evolves_from or "").strip()
            if t_ef.casefold() != prey_key:
                continue
            if not _is_collector_after(pre.collector_number, t.collector_number):
                continue
            candidates.append(t)
        if not candidates:
            continue
        for t in sorted(
            candidates,
            key=lambda c: _collector_sort_key(c.collector_number),
        ):
            # Only one Lucario in set with evolvesFrom Riolu → every Riolu may use that row id.
            if t.id in used_target_ids and n_stage_by_prey[prey_key] > 1:
                continue
            pre.evolves_to_card_id = t.id
            if n_stage_by_prey[prey_key] > 1:
                used_target_ids.add(t.id)
            break


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

    attacks_raw = payload.get("attacks")
    attacks: list[dict[str, Any]] | None = None
    if isinstance(attacks_raw, list) and attacks_raw:
        attacks = []
        for a in attacks_raw:
            if not isinstance(a, dict):
                continue
            nm = a.get("name")
            cost = a.get("cost")
            tx_raw = a.get("text")
            text_out: str | None = None
            if tx_raw is not None:
                tstrip = str(tx_raw).strip()
                text_out = tstrip if tstrip else None
            attacks.append(
                {
                    "name": str(nm).strip() if nm else "Attack",
                    "damage": a.get("damage"),
                    "text": text_out,
                    "cost": cost if isinstance(cost, list) else None,
                }
            )
        if not attacks:
            attacks = None

    ev = payload.get("evolvesTo")
    if isinstance(ev, list):
        evolves_to_names = [str(x).strip() for x in ev if str(x).strip()]
        if not evolves_to_names:
            evolves_to_names = None
    else:
        evolves_to_names = None

    ev_from = payload.get("evolvesFrom")
    if isinstance(ev_from, str) and ev_from.strip():
        evolves_from: str | None = ev_from.strip()
    else:
        evolves_from = None

    types_raw = payload.get("types")
    tcg_types: list[str] | None
    if isinstance(types_raw, list) and types_raw:
        tcg_types = [str(x).strip() for x in types_raw if str(x).strip()]
        if not tcg_types:
            tcg_types = None
    else:
        tcg_types = None

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
        "attacks": attacks,
        "dex_numbers": dex_numbers,
        "tcg_types": tcg_types,
        "rarity_class_id": rarity_class_id,
        "evolves_to_names": evolves_to_names,
        "evolves_from": evolves_from,
    }


async def sync_curated_sets(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    set_ids: list[str],
    api_key: str | None = None,
) -> CatalogSyncResult:
    """Fetch all cards for each set ID and upsert into `cards`."""
    set_ids = filter_excluded_set_codes(set_ids)

    headers: dict[str, str] = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    counts: dict[str, int] = {sid: 0 for sid in set_ids}
    deltas: dict[str, SetSyncStats] = {}

    async with session_factory() as session:
        codes_by_class, rarity_overrides = await _rarity_lookup(session)
        sort_order_by_id = {
            int(rc.id): int(rc.sort_order)
            for rc in (await session.execute(select(RarityClass))).scalars()
        }

        async with httpx.AsyncClient(headers=headers, timeout=120.0) as client:
            for set_id in set_ids:
                had_cards = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(Card)
                        .where(Card.set_code == set_id)
                    )
                    or 0
                )
                stats = SetSyncStats(
                    set_code=set_id,
                    set_name=set_id,
                    is_new_set=had_cards == 0,
                )
                deltas[set_id] = stats
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
                        payload_set = (payload.get("set") or {}).get("id")
                        if is_excluded_set_code(str(payload_set or set_id)):
                            continue

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
                        if values.get("set_name"):
                            stats.set_name = str(values["set_name"])
                        existing = await session.scalar(
                            select(Card).where(Card.tcg_card_id == values["tcg_card_id"])
                        )
                        if existing:
                            for k, v in values.items():
                                setattr(existing, k, v)
                            stats.updated_cards += 1
                        else:
                            card = Card(**values)
                            session.add(card)
                            await session.flush()
                            stats.new_cards += 1
                            stats.note_new_card(
                                card_id=int(card.id),
                                name=str(values.get("name") or "Card"),
                                image_small_url=values.get("image_small_url"),
                                rarity_sort_order=sort_order_by_id.get(int(rid), 0),
                            )

                        counts[set_id] += 1

                    await session.commit()

                    if page * page_size >= total_count or not data:
                        LOG.info(
                            "Finished set %s — stored %s cards (%s new, %s updated; API reports %s)",
                            set_id,
                            counts[set_id],
                            stats.new_cards,
                            stats.updated_cards,
                            total_count,
                        )
                        await _resolve_evolves_for_set(session, set_id)
                        await session.commit()
                        break
                    page += 1

    return CatalogSyncResult(counts=counts, deltas=deltas)


async def sync_query(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    query: str,
    api_key: str | None = None,
    label: str = "query",
) -> dict[str, int]:
    """Fetch all cards matching an API `q=` query and upsert into `cards`.

    Returns `{label: count}`.
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    query = query.strip()
    if not query:
        raise ValueError("query must be non-empty")

    counts: dict[str, int] = {label: 0}

    async with session_factory() as session:
        codes_by_class, rarity_overrides = await _rarity_lookup(session)

        async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
            page = 1
            page_size = 250
            seen_set_codes: set[str] = set()

            while True:
                params = {"q": query, "page": page, "pageSize": page_size}
                resp = await client.get(f"{TCG_BASE}/cards", params=params)
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data") or []
                total_count = int(body.get("totalCount") or 0)

                for payload in data:
                    payload_set = (payload.get("set") or {}).get("id")
                    if is_excluded_set_code(str(payload_set or "")):
                        continue

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
                    if values.get("set_code"):
                        seen_set_codes.add(str(values["set_code"]))
                    existing = await session.scalar(select(Card).where(Card.tcg_card_id == values["tcg_card_id"]))
                    if existing:
                        for k, v in values.items():
                            setattr(existing, k, v)
                    else:
                        session.add(Card(**values))
                    counts[label] += 1

                await session.commit()

                if page * page_size >= total_count or not data:
                    LOG.info(
                        "Finished query %s — stored %s cards (API reports %s)",
                        label,
                        counts[label],
                        total_count,
                    )
                    for set_code in sorted(seen_set_codes):
                        await _resolve_evolves_for_set(session, set_code)
                    await session.commit()
                    break
                page += 1

    return counts
