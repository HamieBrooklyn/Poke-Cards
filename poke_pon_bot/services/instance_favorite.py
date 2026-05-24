"""Per-copy favorites — lock a specific owned instance from transfers."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.inventory import UserCardInstance

FAVORITE_BLOCK = (
    "That copy is **favorited** — unfavorite it in your collection first."
)


def instance_is_favorited(inst: UserCardInstance) -> bool:
    return bool(getattr(inst, "is_favorite", False))


async def toggle_instance_favorite(
    session: AsyncSession,
    *,
    discord_user_id: int,
    instance_id: int,
) -> bool | None:
    """Toggle favorite for an owned copy. Returns new state, or ``None`` if not owned."""
    inst = await session.get(UserCardInstance, instance_id)
    if inst is None or inst.discord_user_id != discord_user_id:
        return None
    inst.is_favorite = not instance_is_favorited(inst)
    await session.flush()
    return inst.is_favorite


async def favorite_instance_ids_among(
    session: AsyncSession,
    instance_ids: set[int],
) -> set[int]:
    """Subset of ``instance_ids`` that are currently favorited."""
    if not instance_ids:
        return set()
    rows = (
        await session.execute(
            select(UserCardInstance.id).where(
                UserCardInstance.id.in_(instance_ids),
                UserCardInstance.is_favorite.is_(True),
            )
        )
    ).scalars()
    return {int(r) for r in rows}
