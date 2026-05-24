"""Assemble owned card pieces into one catalog result (Pokedollars cost)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_assembly import CardAssemblyGroup, CardAssemblyPiece
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.collection_sell import collection_sell_block_reason
from poke_pon_bot.services.collection_visibility import user_instance_not_in_active_auction
from poke_pon_bot.services.duel_engine import parse_hp
from poke_pon_bot.services.instance_public_id import new_public_id, normalize_public_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from poke_pon_bot.services.wallet import WalletService

_ASSEMBLY_MIN_COST = 200
_ASSEMBLY_MAX_COST = 15_000


def _max_attack_damage(attacks: list[dict] | None) -> int:
    if not isinstance(attacks, list):
        return 0
    best = 0
    for atk in attacks:
        if not isinstance(atk, dict):
            continue
        raw = atk.get("damage") or ""
        digits = "".join(ch for ch in str(raw) if ch.isdigit())
        if digits:
            best = max(best, int(digits))
    return best


def quote_assembly_cost(
    result_card: Card,
    result_rarity: RarityClass,
    piece_cards: list[Card],
    piece_rarities: list[RarityClass],
) -> int:
    """Assembly fee from result stats and piece tiers."""
    tier = max(
        int(result_rarity.sort_order),
        *(int(r.sort_order) for r in piece_rarities),
        1,
    )
    base = 100 + tier * 55
    hp = parse_hp(result_card.hp) or 0
    dmg = _max_attack_damage(result_card.attacks)
    pieces_part = len(piece_cards) * 40
    total = base + hp // 5 + dmg * 5 + pieces_part
    return max(_ASSEMBLY_MIN_COST, min(int(total), _ASSEMBLY_MAX_COST))


@dataclass(frozen=True)
class AssemblyPieceInfo:
    slot_index: int
    rotation_deg: int
    grid_col: int
    grid_row: int
    card_id: int
    card_name: str
    image_small_url: str
    image_large_url: str


@dataclass(frozen=True)
class AssemblyGroupInfo:
    id: int
    code: str
    display_name: str
    piece_count: int
    layout: str
    orientation: str
    result_card_id: int
    result_name: str
    result_image_small: str
    result_image_large: str
    pieces: tuple[AssemblyPieceInfo, ...]


async def load_group_info(session: AsyncSession, group_id: int) -> AssemblyGroupInfo | None:
    group = await session.scalar(
        select(CardAssemblyGroup)
        .where(CardAssemblyGroup.id == group_id)
        .options(
            selectinload(CardAssemblyGroup.pieces).selectinload(CardAssemblyPiece.card),
            selectinload(CardAssemblyGroup.result_card),
        )
    )
    if group is None or group.result_card is None:
        return None
    rc = group.result_card
    piece_infos: list[AssemblyPieceInfo] = []
    for p in sorted(group.pieces, key=lambda x: x.slot_index):
        c = p.card
        if c is None:
            continue
        piece_infos.append(
            AssemblyPieceInfo(
                slot_index=p.slot_index,
                rotation_deg=int(p.rotation_deg),
                grid_col=int(p.grid_col),
                grid_row=int(p.grid_row),
                card_id=c.id,
                card_name=c.name,
                image_small_url=c.image_small_url,
                image_large_url=c.image_large_url,
            )
        )
    return AssemblyGroupInfo(
        id=group.id,
        code=group.code,
        display_name=group.display_name,
        piece_count=int(group.piece_count),
        layout=group.layout,
        orientation=group.orientation,
        result_card_id=rc.id,
        result_name=rc.name,
        result_image_small=rc.image_small_url,
        result_image_large=rc.image_large_url,
        pieces=tuple(piece_infos),
    )


async def group_for_card_id(session: AsyncSession, card_id: int) -> tuple[CardAssemblyGroup, int] | None:
    """Return ``(group, slot_index)`` if ``card_id`` is an assembly piece."""
    row = await session.execute(
        select(CardAssemblyPiece, CardAssemblyGroup)
        .join(CardAssemblyGroup, CardAssemblyGroup.id == CardAssemblyPiece.group_id)
        .where(CardAssemblyPiece.card_id == card_id)
    )
    first = row.first()
    if first is None:
        return None
    piece, group = first
    return group, int(piece.slot_index)


def _is_assembled_output(inst: UserCardInstance) -> bool:
    """Instances created by assembly are full cards, not usable puzzle pieces."""
    return (inst.source or "").strip().lower() == "assembly"


async def _load_owned(
    session: AsyncSession,
    *,
    discord_user_id: int,
    public_id: str,
) -> tuple[UserCardInstance, Card] | None:
    pid = normalize_public_id(public_id)
    if pid is None:
        return None
    row = await session.execute(
        select(UserCardInstance, Card)
        .join(Card, Card.id == UserCardInstance.card_id)
        .where(
            UserCardInstance.discord_user_id == discord_user_id,
            UserCardInstance.public_id == pid,
            user_instance_not_in_active_auction(),
        )
    )
    first = row.first()
    return first if first else None


def serialize_group_for_api(info: AssemblyGroupInfo) -> dict[str, Any]:
    return {
        "id": info.id,
        "code": info.code,
        "display_name": info.display_name,
        "piece_count": info.piece_count,
        "layout": info.layout,
        "orientation": info.orientation,
        "result": {
            "card_id": info.result_card_id,
            "name": info.result_name,
            "image_small_url": info.result_image_small,
            "image_large_url": info.result_image_large,
        },
        "slots": [
            {
                "slot_index": p.slot_index,
                "rotation_deg": p.rotation_deg,
                "grid_col": p.grid_col,
                "grid_row": p.grid_row,
                "card_id": p.card_id,
                "name": p.card_name,
                "image_small_url": p.image_small_url,
                "image_large_url": p.image_large_url,
            }
            for p in info.pieces
        ],
    }


async def _resolve_piece_group(
    session: AsyncSession,
    card: Card,
) -> tuple[CardAssemblyGroup, CardAssemblyPiece] | None:
    """Map a catalog printing to its assembly group (V-UNION auto-register or YAML pieces)."""
    from poke_pon_bot.services.assembly_catalog import (
        ensure_vunion_group_for_card,
        is_vunion_card,
    )

    if is_vunion_card(card):
        return await ensure_vunion_group_for_card(session, card)

    row = await session.execute(
        select(CardAssemblyPiece, CardAssemblyGroup)
        .join(CardAssemblyGroup, CardAssemblyGroup.id == CardAssemblyPiece.group_id)
        .where(CardAssemblyPiece.card_id == card.id)
    )
    first = row.first()
    if first is None:
        return None
    return first[0], first[1]


async def list_user_assembly_pieces(
    session: AsyncSession,
    *,
    discord_user_id: int,
    name_contains: str = "",
    anchor_public_id: str | None = None,
) -> list[dict[str, Any]]:
    """Owned instances that are assembly pieces; optional filter by anchor's group.

    Without an anchor, every owned piece is returned (complete set not required).
    With an anchor, only pieces from that assembly group are returned (other slots).
    """
    from poke_pon_bot.services.assembly_catalog import (
        ensure_assembly_registry,
        is_vunion_card,
    )

    await ensure_assembly_registry(session, discord_user_id=discord_user_id)

    anchor_group_id: int | None = None
    anchor_slot: int | None = None
    if anchor_public_id:
        anchor_row = await _load_owned(
            session, discord_user_id=discord_user_id, public_id=anchor_public_id
        )
        if anchor_row:
            _inst, card = anchor_row
            resolved = await _resolve_piece_group(session, card)
            if resolved:
                group, piece = resolved
                anchor_group_id = group.id
                anchor_slot = int(piece.slot_index)

    q = (name_contains or "").strip().lower()
    rows = await session.execute(
        select(UserCardInstance, Card, RarityClass)
        .join(Card, Card.id == UserCardInstance.card_id)
        .join(RarityClass, RarityClass.id == Card.rarity_class_id)
        .where(
            UserCardInstance.discord_user_id == discord_user_id,
            user_instance_not_in_active_auction(),
        )
        .order_by(Card.name.asc(), Card.set_code.asc(), Card.collector_number.asc())
    )

    out: list[dict[str, Any]] = []
    for inst, card, rarity in rows.all():
        if _is_assembled_output(inst):
            continue
        if not is_vunion_card(card):
            # YAML / manual assembly pieces only (non–V-UNION)
            row = await session.execute(
                select(CardAssemblyPiece, CardAssemblyGroup)
                .join(CardAssemblyGroup, CardAssemblyGroup.id == CardAssemblyPiece.group_id)
                .where(CardAssemblyPiece.card_id == card.id)
            )
            manual = row.first()
            if manual is None:
                continue
            piece, group = manual
        else:
            resolved = await _resolve_piece_group(session, card)
            if resolved is None:
                continue
            piece, group = resolved

        if anchor_group_id is not None:
            if group.id != anchor_group_id:
                continue
            if anchor_slot is not None and int(piece.slot_index) == anchor_slot:
                continue
        if q and q not in (card.name or "").lower():
            continue
        blocked = await collection_sell_block_reason(
            session, discord_user_id=discord_user_id, instance_id=inst.id
        )
        out.append(
            {
                "public_id": inst.public_id,
                "obtained_at": inst.obtained_at.isoformat() if inst.obtained_at else None,
                "sell_blocked": blocked,
                "assembly": {
                    "group_id": group.id,
                    "group_code": group.code,
                    "display_name": group.display_name,
                    "slot_index": int(piece.slot_index),
                    "piece_count": int(group.piece_count),
                    "layout": group.layout,
                    "orientation": group.orientation,
                },
                "card": {
                    "name": card.name,
                    "set_code": card.set_code,
                    "set_name": card.set_name,
                    "collector_number": card.collector_number,
                    "image_small_url": card.image_small_url,
                    "image_large_url": card.image_large_url,
                    "rarity": {
                        "code": rarity.code,
                        "display_name": rarity.display_name,
                        "sort_order": int(rarity.sort_order),
                    },
                },
            }
        )
    return out


@dataclass(frozen=True)
class AssemblyQuote:
    cost: int
    display_name: str
    group_id: int


async def quote_assembly_for_public_ids(
    session: AsyncSession,
    *,
    discord_user_id: int,
    public_ids: list[str],
) -> AssemblyQuote | str:
    from poke_pon_bot.services.assembly_catalog import ensure_assembly_registry

    await ensure_assembly_registry(session, discord_user_id=discord_user_id)

    if not public_ids:
        return "Select every piece for this assembly."
    unique = list(dict.fromkeys(public_ids))
    if len(unique) != len(public_ids):
        return "Each piece must be a different copy."

    group_id: int | None = None
    slots_seen: set[int] = set()
    piece_cards: list[Card] = []
    piece_rarities: list[RarityClass] = []

    for pid in unique:
        row = await _load_owned(session, discord_user_id=discord_user_id, public_id=pid)
        if row is None:
            return f"Copy `{pid}` is not in your collection."
        inst, card = row
        if _is_assembled_output(inst):
            return "That copy is a completed assembly — it cannot be used as a puzzle piece."
        blocked = await collection_sell_block_reason(
            session, discord_user_id=discord_user_id, instance_id=inst.id
        )
        if blocked:
            return blocked.replace("**", "")
        resolved = await _resolve_piece_group(session, card)
        if resolved is None:
            return f"**{card.name}** is not an assembly piece."
        piece_row, group = resolved
        slot = int(piece_row.slot_index)
        if group_id is None:
            group_id = group.id
        elif group.id != group_id:
            return "All pieces must belong to the same assembly."
        if slot in slots_seen:
            return "You already selected a piece for that slot."
        slots_seen.add(slot)
        piece_cards.append(card)
        rc = await session.get(RarityClass, card.rarity_class_id)
        if rc:
            piece_rarities.append(rc)

    if group_id is None:
        return "No assembly group found."

    group = await session.get(CardAssemblyGroup, group_id)
    if group is None:
        return "Assembly data is missing — run catalog sync."
    if len(slots_seen) != int(group.piece_count):
        return f"You need **{group.piece_count}** different pieces (have {len(slots_seen)})."

    result_card = await session.get(Card, group.result_card_id)
    result_rarity = None
    if result_card:
        result_rarity = await session.get(RarityClass, result_card.rarity_class_id)
    if result_card is None or result_rarity is None:
        return "Result card data is missing — run catalog sync."

    cost = quote_assembly_cost(result_card, result_rarity, piece_cards, piece_rarities)
    return AssemblyQuote(cost=cost, display_name=group.display_name, group_id=group.id)


@dataclass(frozen=True)
class AssemblySuccess:
    instance: UserCardInstance
    cost: int
    new_balance: int
    group: AssemblyGroupInfo
    consumed_pieces: tuple[dict[str, Any], ...]


async def run_assembly(
    session: AsyncSession,
    wallet: WalletService,
    *,
    discord_user_id: int,
    public_ids: list[str],
) -> AssemblySuccess | str:
    from poke_pon_bot.services.assembly_catalog import ensure_assembly_registry
    from poke_pon_bot.services.wallet import InsufficientPokedollarsError, format_pokedollars

    await ensure_assembly_registry(session, discord_user_id=discord_user_id)

    quote = await quote_assembly_for_public_ids(
        session, discord_user_id=discord_user_id, public_ids=public_ids
    )
    if isinstance(quote, str):
        return quote

    unique = list(dict.fromkeys(public_ids))
    instances: list[UserCardInstance] = []
    consumed_pieces: list[dict[str, Any]] = []
    for pid in unique:
        row = await _load_owned(session, discord_user_id=discord_user_id, public_id=pid)
        if row is None:
            return f"Copy `{pid}` is not in your collection."
        inst, card = row
        resolved = await _resolve_piece_group(session, card)
        if resolved is None:
            return f"**{card.name}** is not an assembly piece."
        piece_row, _group = resolved
        consumed_pieces.append(
            {
                "public_id": inst.public_id,
                "slot_index": int(piece_row.slot_index),
                "rotation_deg": int(piece_row.rotation_deg),
                "grid_col": int(piece_row.grid_col),
                "grid_row": int(piece_row.grid_row),
                "image_small_url": card.image_small_url,
                "image_large_url": card.image_large_url,
                "name": card.name,
            }
        )
        instances.append(inst)

    consumed_pieces.sort(key=lambda p: int(p["slot_index"]))

    group_info = await load_group_info(session, quote.group_id)
    if group_info is None:
        return "Assembly data is missing — run catalog sync."

    try:
        new_balance = await wallet.try_debit(session, discord_user_id, quote.cost)
    except InsufficientPokedollarsError:
        return f"You need **{format_pokedollars(quote.cost)}** to assemble this card."

    for inst in instances:
        await session.delete(inst)

    new_inst = UserCardInstance(
        discord_user_id=discord_user_id,
        public_id=new_public_id(),
        card_id=group_info.result_card_id,
        source="assembly",
        obtained_at=datetime.now(UTC),
    )
    session.add(new_inst)
    await session.flush()

    return AssemblySuccess(
        instance=new_inst,
        cost=quote.cost,
        new_balance=new_balance,
        group=group_info,
        consumed_pieces=tuple(consumed_pieces),
    )
