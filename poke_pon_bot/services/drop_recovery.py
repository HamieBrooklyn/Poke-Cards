"""Save and restore in-flight /cd channel drops across bot restarts."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.pending_channel_drop import PendingChannelDrop

if TYPE_CHECKING:
    from poke_pon_bot.cogs.gacha import PackPickView

_LOG = logging.getLogger(__name__)


def _slot_maps_from_view(view: PackPickView) -> tuple[dict[str, int], dict[str, str]]:
    claimer = {str(k): int(v) for k, v in view._slot_claimer.items()}
    pids = {str(k): str(v) for k, v in view._slot_pid.items()}
    return claimer, pids


def _slot_maps_to_int(claimer: dict[str, int], pids: dict[str, str]) -> tuple[dict[int, int], dict[int, str]]:
    return (
        {int(k): int(v) for k, v in claimer.items()},
        {int(k): str(v) for k, v in pids.items()},
    )


async def upsert_pending_drop(
    session: AsyncSession,
    view: PackPickView,
    *,
    channel_id: int,
    guild_id: int | None,
    content_prefix: str = "",
) -> None:
    """Write the current view state (public channel drops only)."""
    if view._private_pack or view.message is None:
        return
    msg = view.message
    claimer, pids = _slot_maps_from_view(view)
    row = PendingChannelDrop(
        message_id=int(msg.id),
        channel_id=int(channel_id),
        guild_id=guild_id,
        issuer_id=int(view._issuer_id),
        opener_mention=str(view._opener_mention)[:256],
        card_ids=[int(c.id) for c in view._cards],
        deadline_unix=int(view._deadline_unix),
        claim_seconds=max(30, int(getattr(view, "_claim_seconds_total", 180) or 180)),
        max_grabs_per_user=int(view._max_grabs_per_user),
        private_pack=False,
        mission_block=str(view._mission_block or ""),
        content_prefix=str(content_prefix or ""),
        slot_claimer=claimer,
        slot_public_ids=pids,
        finished=bool(view._finished),
    )
    await session.merge(row)


async def delete_pending_drop(session: AsyncSession, message_id: int) -> None:
    await session.execute(
        delete(PendingChannelDrop).where(PendingChannelDrop.message_id == message_id)
    )


async def mark_pending_drop_finished(session: AsyncSession, message_id: int) -> None:
    row = await session.get(PendingChannelDrop, message_id)
    if row is None:
        return
    row.finished = True


async def list_restorable_drops(session: AsyncSession) -> list[PendingChannelDrop]:
    now = int(time.time())
    res = await session.execute(
        select(PendingChannelDrop).where(
            PendingChannelDrop.finished.is_(False),
            PendingChannelDrop.private_pack.is_(False),
            PendingChannelDrop.deadline_unix > now,
        )
    )
    return list(res.scalars().all())


async def list_expired_pending_drops(session: AsyncSession) -> list[PendingChannelDrop]:
    now = int(time.time())
    res = await session.execute(
        select(PendingChannelDrop).where(
            PendingChannelDrop.finished.is_(False),
            PendingChannelDrop.deadline_unix <= now,
        )
    )
    return list(res.scalars().all())


async def load_cards_for_drop(session: AsyncSession, card_ids: list[int]) -> list[Card] | None:
    if not card_ids:
        return None
    res = await session.execute(select(Card).where(Card.id.in_(card_ids)))
    by_id = {int(c.id): c for c in res.scalars().all()}
    ordered = [by_id[i] for i in card_ids if i in by_id]
    if len(ordered) != len(card_ids):
        return None
    return ordered


async def flush_active_channel_drops(
    session_factory: async_sessionmaker[AsyncSession],
    active_views: dict[int, PackPickView],
    *,
    content_prefix_by_message: dict[int, str] | None = None,
) -> int:
    """Persist every registered active drop (shutdown / crash-save hook)."""
    prefixes = content_prefix_by_message or {}
    saved = 0
    async with session_factory() as session:
        for mid, view in list(active_views.items()):
            if view._private_pack or view.message is None:
                continue
            ch = getattr(view, "_channel_id", None)
            if ch is None:
                continue
            await upsert_pending_drop(
                session,
                view,
                channel_id=int(ch),
                guild_id=getattr(view, "_guild_id", None),
                content_prefix=prefixes.get(mid, getattr(view, "_content_prefix", "")),
            )
            saved += 1
        await session.commit()
    if saved:
        _LOG.info("Persisted %s active channel drop(s) before shutdown.", saved)
    return saved


async def finalize_expired_drop_row(
    session: AsyncSession,
    row: PendingChannelDrop,
    bot: Any,
) -> None:
    """Disable buttons on an expired drop message and remove the DB row."""
    row.finished = True
    channel = bot.get_channel(int(row.channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(row.channel_id))
        except Exception:
            channel = None
    if channel is None:
        await delete_pending_drop(session, int(row.message_id))
        return
    try:
        msg = await channel.fetch_message(int(row.message_id))
    except Exception:
        await delete_pending_drop(session, int(row.message_id))
        return
    n_claimed = len(row.slot_public_ids or {})
    n_cards = len(row.card_ids or [])
    extra = (
        "\n\n⏱ **Time's up** — unclaimed cards are gone.\n"
        f"_Claimed **{n_claimed}** / **{n_cards}**._"
    )
    try:
        base = (msg.content or "").split("\n\n⏱ **Time's up**")[0].rstrip()
        await msg.edit(content=base + extra, view=None)
    except Exception:
        _LOG.debug("Could not edit expired drop message %s", row.message_id, exc_info=True)
    await delete_pending_drop(session, int(row.message_id))
