"""Weighted random pulls from the catalog."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.drops import DropTable, DropWeight
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.instance_public_id import new_public_id
from poke_pon_bot.services.weighted_rng import weighted_choice

# After the first two guaranteed rolls: probability of each next card appearing,
# then stop (fourth slot rarer than third, etc.).
_EXTRA_CARD_CHANCES: tuple[float, ...] = (0.10, 0.03, 0.01, 0.003, 0.001)
_MAX_PACK_SIZE = min(25, 2 + len(_EXTRA_CARD_CHANCES))


@dataclass(frozen=True)
class DropResult:
    card: Card
    instance_id: int
    public_id: str


class DropService:
    """Pick a rarity tier from `drop_weights`, then a random card in that tier."""

    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random(secrets.randbits(128))

    async def draw_single_card(
        self,
        session: AsyncSession,
        *,
        drop_table_code: str = "default",
    ) -> Card:
        """Roll one catalog card using tier weights (does not touch inventory)."""
        drop_table = await session.scalar(
            select(DropTable).where(DropTable.code == drop_table_code)
        )
        if drop_table is None:
            msg = f"Unknown drop table {drop_table_code!r}"
            raise LookupError(msg)

        weight_result = await session.execute(
            select(DropWeight.rarity_class_id, DropWeight.weight).where(
                DropWeight.drop_table_id == drop_table.id
            )
        )
        weights_list = weight_result.all()
        if not weights_list:
            raise LookupError("Drop table has no weights configured.")

        eligible_classes = await session.execute(select(Card.rarity_class_id.distinct()))
        eligible_ids = {int(x) for x in eligible_classes.scalars()}

        pruned = [(rid, float(w)) for rid, w in weights_list if int(rid) in eligible_ids]
        if not pruned:
            raise RuntimeError(
                "No cards in the catalog match configured rarities — run `python -m poke_pon_bot.scripts.sync_catalog`."
            )

        rarity_class_id = weighted_choice(self._rng, pruned)

        card = await session.scalar(
            select(Card)
            .where(Card.rarity_class_id == rarity_class_id)
            .order_by(func.random())
            .limit(1)
        )
        if card is None:
            raise RuntimeError("Could not roll a card — catalog may be incomplete.")

        return card

    async def roll_pack(
        self,
        session: AsyncSession,
        *,
        drop_table_code: str = "default",
    ) -> list[Card]:
        """Open a pack: always 2 cards, then optional extras with diminishing odds."""
        pack: list[Card] = [
            await self.draw_single_card(session, drop_table_code=drop_table_code),
            await self.draw_single_card(session, drop_table_code=drop_table_code),
        ]

        extras = 0
        while extras < len(_EXTRA_CARD_CHANCES) and len(pack) < _MAX_PACK_SIZE:
            if self._rng.random() >= _EXTRA_CARD_CHANCES[extras]:
                break
            pack.append(await self.draw_single_card(session, drop_table_code=drop_table_code))
            extras += 1

        return pack

    async def claim_card(
        self,
        session: AsyncSession,
        *,
        discord_user_id: int,
        card: Card,
        source: str = "drop",
    ) -> DropResult:
        """Persist a single chosen card into the user inventory."""
        for _ in range(12):
            row = UserCardInstance(
                public_id=new_public_id(),
                discord_user_id=discord_user_id,
                card_id=card.id,
                source=source,
            )
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                # Vanishingly rare id clash; new_public_id and retry. Savepoint spares a full rollback.
                continue
            return DropResult(card=card, instance_id=row.id, public_id=row.public_id)
        msg = "Could not assign a unique Card ID — try again."
        raise RuntimeError(msg)

    async def pull(
        self,
        session: AsyncSession,
        *,
        discord_user_id: int,
        drop_table_code: str = "default",
        source: str = "drop",
    ) -> DropResult:
        """Single pull that immediately stores in inventory (legacy helper)."""
        card = await self.draw_single_card(session, drop_table_code=drop_table_code)
        return await self.claim_card(
            session,
            discord_user_id=discord_user_id,
            card=card,
            source=source,
        )
