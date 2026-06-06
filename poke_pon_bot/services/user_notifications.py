"""Persist and query the website notification inbox."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.user_notification import UserNotification


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).isoformat()
    return dt.astimezone(UTC).isoformat()


def serialize_notification(row: UserNotification) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "href": row.href,
        "read": row.read_at is not None,
        "read_at": _utc_iso(row.read_at),
        "created_at": _utc_iso(row.created_at),
    }


async def create_notification(
    session: AsyncSession,
    *,
    discord_user_id: int,
    kind: str,
    title: str,
    body: str,
    href: str | None = None,
) -> UserNotification:
    row = UserNotification(
        discord_user_id=int(discord_user_id),
        kind=str(kind)[:32],
        title=str(title)[:256],
        body=str(body)[:4000],
        href=(str(href)[:512] if href else None),
    )
    session.add(row)
    await session.flush()
    return row


async def list_notifications(
    session: AsyncSession,
    discord_user_id: int,
    *,
    limit: int = 40,
    unread_only: bool = False,
) -> list[UserNotification]:
    cap = max(1, min(int(limit), 100))
    stmt = (
        select(UserNotification)
        .where(UserNotification.discord_user_id == int(discord_user_id))
        .order_by(desc(UserNotification.created_at))
        .limit(cap)
    )
    if unread_only:
        stmt = stmt.where(UserNotification.read_at.is_(None))
    rows = await session.execute(stmt)
    return list(rows.scalars())


async def unread_count(session: AsyncSession, discord_user_id: int) -> int:
    n = await session.scalar(
        select(func.count(UserNotification.id)).where(
            UserNotification.discord_user_id == int(discord_user_id),
            UserNotification.read_at.is_(None),
        )
    )
    return int(n or 0)


async def mark_notifications_read(
    session: AsyncSession,
    discord_user_id: int,
    *,
    notification_ids: list[int] | None = None,
    mark_all: bool = False,
) -> int:
    now = datetime.now(UTC)
    stmt = (
        update(UserNotification)
        .where(
            UserNotification.discord_user_id == int(discord_user_id),
            UserNotification.read_at.is_(None),
        )
        .values(read_at=now)
    )
    if not mark_all:
        ids = [int(i) for i in (notification_ids or []) if int(i) > 0]
        if not ids:
            return 0
        stmt = stmt.where(UserNotification.id.in_(ids))
    result = await session.execute(stmt)
    return int(result.rowcount or 0)
