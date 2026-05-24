"""Batch lookups for assembly metadata on collection rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from poke_pon_bot.models.card_assembly import CardAssemblyGroup, CardAssemblyPiece


async def piece_meta_by_card_ids(
    session: AsyncSession,
    card_ids: set[int],
) -> dict[int, dict[str, Any]]:
    if not card_ids:
        return {}
    rows = await session.execute(
        select(CardAssemblyPiece, CardAssemblyGroup)
        .join(CardAssemblyGroup, CardAssemblyGroup.id == CardAssemblyPiece.group_id)
        .where(CardAssemblyPiece.card_id.in_(card_ids))
    )
    out: dict[int, dict[str, Any]] = {}
    for piece, group in rows.all():
        out[int(piece.card_id)] = {
            "role": "piece",
            "group_id": group.id,
            "group_code": group.code,
            "display_name": group.display_name,
            "slot_index": int(piece.slot_index),
            "piece_count": int(group.piece_count),
            "layout": group.layout,
            "orientation": group.orientation,
        }
    return out


async def result_meta_by_card_ids(
    session: AsyncSession,
    card_ids: set[int],
) -> dict[int, dict[str, Any]]:
    if not card_ids:
        return {}
    groups = (
        await session.execute(
            select(CardAssemblyGroup)
            .where(CardAssemblyGroup.result_card_id.in_(card_ids))
            .options(selectinload(CardAssemblyGroup.pieces).selectinload(CardAssemblyPiece.card))
        )
    ).scalars().all()
    out: dict[int, dict[str, Any]] = {}
    for group in groups:
        slots = []
        for p in sorted(group.pieces, key=lambda x: x.slot_index):
            c = p.card
            if c is None:
                continue
            slots.append(
                {
                    "slot_index": p.slot_index,
                    "rotation_deg": int(p.rotation_deg),
                    "grid_col": int(p.grid_col),
                    "grid_row": int(p.grid_row),
                    "image_small_url": c.image_small_url,
                    "image_large_url": c.image_large_url,
                }
            )
        out[int(group.result_card_id)] = {
            "role": "result",
            "group_id": group.id,
            "group_code": group.code,
            "display_name": group.display_name,
            "piece_count": int(group.piece_count),
            "layout": group.layout,
            "orientation": group.orientation,
            "slots": slots,
        }
    return out
