"""Discover and upsert card assembly groups from the catalog + YAML overrides."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_assembly import CardAssemblyGroup, CardAssemblyPiece
from poke_pon_bot.services.card_roles import card_subtypes
from poke_pon_bot.services.catalog_sync import _collector_sort_key
from poke_pon_bot.services.duel_engine import parse_hp

LOG = logging.getLogger(__name__)

_V_UNION_SUBTYPE = "v-union"
_LAYOUT_QUAD = "quad"
_LAYOUT_HALVES = "horizontal_halves"


def _slug_code(*parts: str) -> str:
    raw = "_".join(p.strip().lower() for p in parts if p and str(p).strip())
    raw = re.sub(r"[^a-z0-9]+", "_", raw)
    return raw.strip("_")[:120] or "assembly"


def _has_vunion_subtype(card: Card) -> bool:
    return any(s.casefold() == _V_UNION_SUBTYPE for s in card_subtypes(card))


def is_vunion_card(card: Card) -> bool:
    """V-UNION pieces are identified by API subtype and/or printed name."""
    if _has_vunion_subtype(card):
        return True
    return "v-union" in (card.name or "").casefold()


def _pick_result_card(cards: list[Card]) -> Card:
    def score(c: Card) -> tuple[int, int]:
        hp = parse_hp(c.hp) or 0
        atks = len(c.attacks) if isinstance(c.attacks, list) else 0
        return (hp, atks)

    return max(cards, key=score)


def _quad_grid(slot_index: int) -> tuple[int, int]:
    """TL, TR, BL, BR for a 2×2 V-UNION board."""
    return (slot_index % 2, slot_index // 2)


def _halves_grid(slot_index: int) -> tuple[int, int]:
    return (slot_index, 0)


async def _upsert_group(
    session: AsyncSession,
    *,
    code: str,
    display_name: str,
    result_card: Card,
    piece_cards: list[Card],
    layout: str,
    orientation: str,
) -> CardAssemblyGroup:
    piece_count = len(piece_cards)
    if piece_count not in (2, 4):
        msg = f"Assembly {code!r} needs 2 or 4 pieces, got {piece_count}"
        raise ValueError(msg)

    existing = await session.scalar(
        select(CardAssemblyGroup).where(CardAssemblyGroup.code == code)
    )
    if existing is None:
        group = CardAssemblyGroup(
            code=code,
            display_name=display_name,
            result_card_id=result_card.id,
            piece_count=piece_count,
            layout=layout,
            orientation=orientation,
        )
        session.add(group)
        await session.flush()
    else:
        group = existing
        group.display_name = display_name
        group.result_card_id = result_card.id
        group.piece_count = piece_count
        group.layout = layout
        group.orientation = orientation
        await session.execute(
            delete(CardAssemblyPiece).where(CardAssemblyPiece.group_id == group.id)
        )
        await session.flush()

    grid_fn = _quad_grid if layout == _LAYOUT_QUAD else _halves_grid
    for idx, card in enumerate(piece_cards):
        col, row = grid_fn(idx)
        session.add(
            CardAssemblyPiece(
                group_id=group.id,
                card_id=card.id,
                slot_index=idx,
                rotation_deg=0,
                grid_col=col,
                grid_row=row,
            )
        )
    return group


async def _upsert_vunion_chunks(
    session: AsyncSession,
    by_key: dict[tuple[str, str], list[Card]],
) -> int:
    created = 0
    for (set_code, name), cards in by_key.items():
        if len(cards) < 4:
            continue
        ordered = sorted(cards, key=lambda c: _collector_sort_key(c.collector_number))
        for chunk_start in range(0, len(ordered), 4):
            chunk = ordered[chunk_start : chunk_start + 4]
            if len(chunk) != 4:
                continue
            first_num = chunk[0].collector_number
            code = _slug_code(name, set_code, first_num)
            result = _pick_result_card(chunk)
            await _upsert_group(
                session,
                code=code,
                display_name=name,
                result_card=result,
                piece_cards=chunk,
                layout=_LAYOUT_QUAD,
                orientation="portrait",
            )
            created += 1
    return created


async def sync_vunion_groups_from_catalog(session: AsyncSession) -> int:
    """Build assembly groups for every 4-card V-UNION set in the catalog."""
    rows = (await session.execute(select(Card))).scalars().all()
    vunion = [c for c in rows if is_vunion_card(c)]
    by_key: dict[tuple[str, str], list[Card]] = {}
    for card in vunion:
        key = (card.set_code, (card.name or "").strip())
        by_key.setdefault(key, []).append(card)

    created = await _upsert_vunion_chunks(session, by_key)
    await session.flush()
    LOG.info("Synced %s V-UNION assembly group(s) from catalog.", created)
    return created


async def sync_vunion_groups_for_user_owned(
    session: AsyncSession,
    *,
    discord_user_id: int,
) -> int:
    """Ensure V-UNION groups exist for sets the user actually owns pieces from."""
    from poke_pon_bot.models.inventory import UserCardInstance
    from poke_pon_bot.services.collection_visibility import user_instance_not_in_active_auction

    owned = (
        await session.execute(
            select(Card)
            .join(UserCardInstance, UserCardInstance.card_id == Card.id)
            .where(
                UserCardInstance.discord_user_id == discord_user_id,
                user_instance_not_in_active_auction(),
            )
        )
    ).scalars().all()
    keys: set[tuple[str, str]] = set()
    for card in owned:
        if not is_vunion_card(card):
            continue
        keys.add((card.set_code, (card.name or "").strip()))

    if not keys:
        return 0

    by_key: dict[tuple[str, str], list[Card]] = {}
    for set_code, name in keys:
        catalog_cards = (
            await session.execute(
                select(Card).where(
                    Card.set_code == set_code,
                    Card.name == name,
                )
            )
        ).scalars().all()
        vunion = [c for c in catalog_cards if is_vunion_card(c)]
        if vunion:
            by_key[(set_code, name)] = vunion

    created = await _upsert_vunion_chunks(session, by_key)
    if created:
        await session.flush()
        LOG.info(
            "Synced %s V-UNION assembly group(s) for user %s owned sets.",
            created,
            discord_user_id,
        )
    return created


async def load_yaml_assemblies(
    session: AsyncSession,
    path: Path | None = None,
) -> int:
    """Apply manual recipes from ``config/card_assemblies.yaml``."""
    if path is None:
        path = Path.cwd() / "config" / "card_assemblies.yaml"
    if not path.is_file():
        return 0

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("assemblies") or []
    if not isinstance(entries, list):
        return 0

    loaded = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip()
        if not code:
            continue
        result_tcg = str(entry.get("result_tcg_card_id") or "").strip()
        layout = str(entry.get("layout") or _LAYOUT_QUAD).strip()
        orientation = str(entry.get("orientation") or "portrait").strip()
        display_name = str(entry.get("display_name") or code).strip()
        raw_pieces = entry.get("pieces") or []
        if not result_tcg or not isinstance(raw_pieces, list) or len(raw_pieces) not in (2, 4):
            LOG.warning("Skipping assembly %s — invalid pieces or result id.", code)
            continue

        result_card = await session.scalar(
            select(Card).where(Card.tcg_card_id == result_tcg)
        )
        if result_card is None:
            LOG.warning("Assembly %s: result tcg_card_id %s not in catalog.", code, result_tcg)
            continue

        piece_cards: list[Card] = []
        for p in raw_pieces:
            if isinstance(p, str):
                tcg_id = p.strip()
                rot = 0
            elif isinstance(p, dict):
                tcg_id = str(p.get("tcg_card_id") or "").strip()
                rot = int(p.get("rotation_deg") or 0)
            else:
                continue
            card = await session.scalar(select(Card).where(Card.tcg_card_id == tcg_id))
            if card is None:
                LOG.warning("Assembly %s: piece %s missing from catalog.", code, tcg_id)
                piece_cards = []
                break
            piece_cards.append(card)

        if len(piece_cards) not in (2, 4):
            continue

        group = await _upsert_group(
            session,
            code=code,
            display_name=display_name,
            result_card=result_card,
            piece_cards=piece_cards,
            layout=layout,
            orientation=orientation,
        )
        # Apply per-piece rotation from YAML
        pieces_rows = (
            await session.execute(
                select(CardAssemblyPiece).where(CardAssemblyPiece.group_id == group.id)
            )
        ).scalars().all()
        for idx, piece_row in enumerate(pieces_rows):
            raw = raw_pieces[idx]
            if isinstance(raw, dict) and "rotation_deg" in raw:
                piece_row.rotation_deg = int(raw.get("rotation_deg") or 0)
        loaded += 1

    await session.flush()
    if loaded:
        LOG.info("Loaded %s assembly group(s) from %s.", loaded, path)
    return loaded


async def sync_all_assemblies(session: AsyncSession, *, yaml_path: Path | None = None) -> int:
    """V-UNION discovery plus YAML recipes."""
    n_v = await sync_vunion_groups_from_catalog(session)
    n_y = await load_yaml_assemblies(session, yaml_path)
    return n_v + n_y


async def ensure_vunion_group_for_card(
    session: AsyncSession,
    card: Card,
) -> tuple[CardAssemblyGroup, CardAssemblyPiece] | None:
    """Ensure this V-UNION printing has piece + group rows; create from catalog siblings if missing."""
    if not is_vunion_card(card):
        return None

    row = await session.execute(
        select(CardAssemblyPiece, CardAssemblyGroup)
        .join(CardAssemblyGroup, CardAssemblyGroup.id == CardAssemblyPiece.group_id)
        .where(CardAssemblyPiece.card_id == card.id)
    )
    first = row.first()
    if first is not None:
        return first[0], first[1]

    siblings = (
        await session.execute(
            select(Card).where(
                Card.set_code == card.set_code,
                Card.name == card.name,
            )
        )
    ).scalars().all()
    vunion_siblings = [c for c in siblings if is_vunion_card(c)]
    if len(vunion_siblings) < 4:
        return None

    ordered = sorted(vunion_siblings, key=lambda c: _collector_sort_key(c.collector_number))
    for chunk_start in range(0, len(ordered), 4):
        chunk = ordered[chunk_start : chunk_start + 4]
        if len(chunk) != 4:
            continue
        chunk_ids = {c.id for c in chunk}
        if card.id not in chunk_ids:
            continue
        code = _slug_code(card.name, card.set_code, chunk[0].collector_number)
        result = _pick_result_card(chunk)
        group = await _upsert_group(
            session,
            code=code,
            display_name=card.name,
            result_card=result,
            piece_cards=chunk,
            layout=_LAYOUT_QUAD,
            orientation="portrait",
        )
        piece = await session.scalar(
            select(CardAssemblyPiece).where(
                CardAssemblyPiece.group_id == group.id,
                CardAssemblyPiece.card_id == card.id,
            )
        )
        if piece is not None:
            return group, piece
    return None


async def ensure_assembly_registry(
    session: AsyncSession,
    *,
    discord_user_id: int | None = None,
) -> None:
    """Idempotent: register assembly groups before listing or combining pieces."""
    await sync_all_assemblies(session)
    if discord_user_id is not None:
        await sync_vunion_groups_for_user_owned(
            session, discord_user_id=discord_user_id
        )
