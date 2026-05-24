"""Weighted random pulls from the catalog."""

from __future__ import annotations

import random
import secrets
from collections.abc import Iterable, Mapping

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.drops import DropTable, DropWeight
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.cd_drop_themes import (
    ResolvedCdDropTheme,
    resolve_active_cd_drop_theme,
    theme_applies_for_slot,
)
from poke_pon_bot.services.rarity_luck import luck_rarity_weight_multiplier
from poke_pon_bot.services.rarity_luck_boost import resolve_effective_rarity_luck
from poke_pon_bot.services.excluded_sets import (
    excluded_set_clause,
    filter_excluded_set_codes,
)
from poke_pon_bot.services.instance_public_id import new_public_id
from poke_pon_bot.services.weighted_rng import weighted_choice

# After the first two guaranteed rolls: probability of each next card appearing,
# then stop (fourth slot rarer than third, etc.).
_EXTRA_CARD_CHANCES: tuple[float, ...] = (0.10, 0.03, 0.01, 0.003, 0.001)
_MAX_PACK_SIZE = min(25, 2 + len(_EXTRA_CARD_CHANCES))
# Fixed-size packs (e.g. ``/dev drop``) may exceed the random extra-card cap.
_FIXED_PACK_SIZE_CAP = 25

# Booster pack: avoid the same printing filling most slots; try this many redraws per slot.
_PACK_DEDUP_ATTEMPTS = 18

# Extra rarity luck applied only to booster series opens (not /cd — /cd uses base drop_weights only).
PACK_OPEN_LUCK_PERCENT = 36.0

# The code-card slot (11th card when 10+1) uses this multiplier on pack luck (price bonus included).
CODE_SLOT_LUCK_MULTIPLIER = 2.0

# Pricier series add luck on top of the base (crystals above this baseline).
PACK_PRICE_LUCK_BASE_CRYSTALS = 9
PACK_PRICE_LUCK_PER_CRYSTAL_ABOVE_BASE = 2.0

# Bonus code card: bias rarity weights upward (same tiers, higher relative odds for holo+).
def pack_price_luck_bonus(crystal_price: int) -> float:
    """Extra +luck% for expensive boosters (stacks with :data:`PACK_OPEN_LUCK_PERCENT`)."""
    above = max(0, int(crystal_price) - PACK_PRICE_LUCK_BASE_CRYSTALS)
    return float(above) * PACK_PRICE_LUCK_PER_CRYSTAL_ABOVE_BASE


def pack_slot_luck_percent(
    *,
    crystal_price: int,
    code_slot: bool,
    base_luck_percent: float = PACK_OPEN_LUCK_PERCENT,
) -> float:
    """Total pack-open luck for one slot type (before server/guild boosts)."""
    core = float(base_luck_percent) + pack_price_luck_bonus(crystal_price)
    if code_slot:
        return core * CODE_SLOT_LUCK_MULTIPLIER
    return core


_CODE_SLOT_RARITY_WEIGHT_MULT: Mapping[int, float] = {
    1: 1.0,
    2: 1.04,
    3: 1.12,
    4: 1.28,
    5: 1.55,
    6: 1.85,
    7: 2.25,
    8: 2.75,
    9: 3.35,
    10: 4.0,
}


@dataclass(frozen=True)
class DropResult:
    card: Card
    instance_id: int
    public_id: str


class DropService:
    """Pick a rarity tier from `drop_weights`, then a random card in that tier."""

    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random(secrets.randbits(128))

    def _prune_weights_for_scope(
        self,
        weights_list: list[tuple[int, float]],
        *,
        eligible_ids: set[int],
        apply_code_slot_rarity_bias: bool,
        luck_percent: float = 0.0,
    ) -> list[tuple[int, float]]:
        pruned = [(rid, float(w)) for rid, w in weights_list if int(rid) in eligible_ids]
        if not pruned:
            return []
        out: list[tuple[int, float]] = []
        for rid, w in pruned:
            mult = 1.0
            if apply_code_slot_rarity_bias:
                mult *= float(_CODE_SLOT_RARITY_WEIGHT_MULT.get(int(rid), 1.0))
            if luck_percent != 0:
                mult *= luck_rarity_weight_multiplier(int(rid), luck_percent)
            out.append((rid, w * mult))
        return out

    async def draw_single_card(
        self,
        session: AsyncSession,
        *,
        drop_table_code: str = "default",
        set_codes: Iterable[str] | None = None,
        exclude_card_ids: Iterable[int] | None = None,
        apply_code_slot_rarity_bias: bool = False,
        luck_percent: float = 0.0,
        name_contains: str | None = None,
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

        scoped_codes: list[str] | None = None
        if set_codes is not None:
            scoped_codes = sorted(set(filter_excluded_set_codes(set_codes)))
            if not scoped_codes:
                raise RuntimeError(
                    "No set_codes provided for a scoped pull — refusing to silently fall back to global."
                )

        eligible_stmt = select(Card.rarity_class_id.distinct()).where(
            excluded_set_clause(Card.set_code)
        )
        if scoped_codes is not None:
            eligible_stmt = eligible_stmt.where(Card.set_code.in_(scoped_codes))
        name_q = name_contains.strip().lower() if name_contains and name_contains.strip() else None
        if name_q:
            eligible_stmt = eligible_stmt.where(func.lower(Card.name).like(f"%{name_q}%"))
        eligible_classes = await session.execute(eligible_stmt)
        eligible_ids = {int(x) for x in eligible_classes.scalars()}

        pruned = self._prune_weights_for_scope(
            weights_list,
            eligible_ids=eligible_ids,
            apply_code_slot_rarity_bias=apply_code_slot_rarity_bias,
            luck_percent=luck_percent,
        )
        if not pruned:
            if scoped_codes is not None:
                raise RuntimeError(
                    "No cards match the configured rarities **inside** "
                    f"sets {scoped_codes!r} — run `python -m poke_pon_bot.scripts.sync_catalog`."
                )
            raise RuntimeError(
                "No cards in the catalog match configured rarities — run `python -m poke_pon_bot.scripts.sync_catalog`."
            )

        rarity_class_id = weighted_choice(self._rng, pruned)

        exclude: list[int] | None = None
        if exclude_card_ids is not None:
            exclude = sorted({int(x) for x in exclude_card_ids if int(x) > 0})
            if not exclude:
                exclude = None

        def _pick_stmt(with_exclude: bool):
            s = (
                select(Card)
                .where(Card.rarity_class_id == rarity_class_id)
                .where(excluded_set_clause(Card.set_code))
                .order_by(func.random())
                .limit(1)
            )
            if scoped_codes is not None:
                s = s.where(Card.set_code.in_(scoped_codes))
            if name_q:
                s = s.where(func.lower(Card.name).like(f"%{name_q}%"))
            if with_exclude and exclude:
                s = s.where(Card.id.notin_(exclude))
            return s

        card = await session.scalar(_pick_stmt(with_exclude=True))
        if card is None and exclude:
            card = await session.scalar(_pick_stmt(with_exclude=False))
        if card is None:
            raise RuntimeError("Could not roll a card — catalog may be incomplete.")

        return card

    async def draw_single_pokemon(
        self,
        session: AsyncSession,
        *,
        drop_table_code: str = "default",
        luck_percent: float = 0.0,
    ) -> Card:
        """Roll one **Pokémon** catalog card (HP > 0) using tier weights (does not touch inventory)."""
        drop_table = await session.scalar(select(DropTable).where(DropTable.code == drop_table_code))
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

        eligible_classes = await session.execute(
            select(Card.rarity_class_id.distinct())
            .where(Card.supertype == "Pokémon")
            .where(Card.hp.isnot(None))
            .where(excluded_set_clause(Card.set_code))
        )
        eligible_ids = {int(x) for x in eligible_classes.scalars()}
        pruned = self._prune_weights_for_scope(
            weights_list,
            eligible_ids=eligible_ids,
            apply_code_slot_rarity_bias=False,
            luck_percent=luck_percent,
        )
        if not pruned:
            raise RuntimeError(
                "No Pokémon in the catalog match configured rarities — run `python -m poke_pon_bot.scripts.sync_catalog`."
            )

        rarity_class_id = weighted_choice(self._rng, pruned)
        card = await session.scalar(
            select(Card)
            .where(Card.rarity_class_id == rarity_class_id)
            .where(Card.supertype == "Pokémon")
            .where(Card.hp.isnot(None))
            .where(excluded_set_clause(Card.set_code))
            .order_by(func.random())
            .limit(1)
        )
        if card is None:
            card = await session.scalar(
                select(Card)
                .where(Card.supertype == "Pokémon")
                .where(Card.hp.isnot(None))
                .where(excluded_set_clause(Card.set_code))
                .order_by(func.random())
                .limit(1)
            )
        if card is None:
            raise RuntimeError("Could not roll a Pokémon — catalog may be incomplete.")
        return card

    async def _draw_cd_slot(
        self,
        session: AsyncSession,
        *,
        drop_table_code: str,
        luck_percent: float,
        theme: ResolvedCdDropTheme | None,
    ) -> Card:
        """One ``/cd`` slot, optionally forced to the active global/server theme."""
        if theme is not None and theme_applies_for_slot(theme.chance_percent, self._rng):
            try:
                return await self.draw_single_card(
                    session,
                    drop_table_code=drop_table_code,
                    set_codes=list(theme.set_codes) if theme.set_codes else None,
                    name_contains=theme.name_contains,
                    luck_percent=luck_percent,
                )
            except RuntimeError:
                pass
        return await self.draw_single_card(
            session,
            drop_table_code=drop_table_code,
            luck_percent=luck_percent,
        )

    async def roll_pack(
        self,
        session: AsyncSession,
        *,
        drop_table_code: str = "default",
        card_count: int | None = None,
        luck_percent: float = 0.0,
        guild_id: int | None = None,
    ) -> list[Card]:
        """Open a pack: default is 2 cards + optional extras; ``card_count`` fixes the size."""
        luck = await resolve_effective_rarity_luck(
            session,
            guild_id,
            extra_luck_percent=luck_percent,
        )
        theme = await resolve_active_cd_drop_theme(session, guild_id)

        if card_count is not None:
            n = max(2, min(int(card_count), _FIXED_PACK_SIZE_CAP))
            return [
                await self._draw_cd_slot(
                    session,
                    drop_table_code=drop_table_code,
                    luck_percent=luck,
                    theme=theme,
                )
                for _ in range(n)
            ]

        pack: list[Card] = [
            await self._draw_cd_slot(
                session, drop_table_code=drop_table_code, luck_percent=luck, theme=theme
            ),
            await self._draw_cd_slot(
                session, drop_table_code=drop_table_code, luck_percent=luck, theme=theme
            ),
        ]

        extras = 0
        while extras < len(_EXTRA_CARD_CHANCES) and len(pack) < _MAX_PACK_SIZE:
            if self._rng.random() >= _EXTRA_CARD_CHANCES[extras]:
                break
            pack.append(
                await self._draw_cd_slot(
                    session,
                    drop_table_code=drop_table_code,
                    luck_percent=luck,
                    theme=theme,
                )
            )
            extras += 1

        return pack

    async def _draw_pack_slot(
        self,
        session: AsyncSession,
        *,
        set_codes: list[str],
        drop_table_code: str,
        pulled_ids: set[int],
        code_slot: bool,
        luck_percent: float = 0.0,
    ) -> Card:
        """One booster slot: avoid repeating the same printing until the pool is exhausted."""
        for attempt in range(_PACK_DEDUP_ATTEMPTS):
            use_exclude = (
                pulled_ids
                if pulled_ids and attempt < _PACK_DEDUP_ATTEMPTS - 1
                else None
            )
            card = await self.draw_single_card(
                session,
                drop_table_code=drop_table_code,
                set_codes=set_codes,
                exclude_card_ids=use_exclude,
                apply_code_slot_rarity_bias=code_slot,
                luck_percent=luck_percent,
            )
            if use_exclude is None or card.id not in pulled_ids:
                return card
        return await self.draw_single_card(
            session,
            drop_table_code=drop_table_code,
            set_codes=set_codes,
            exclude_card_ids=None,
            apply_code_slot_rarity_bias=code_slot,
            luck_percent=luck_percent,
        )

    async def roll_pack_for_series(
        self,
        session: AsyncSession,
        *,
        set_codes: Iterable[str],
        cards_count: int = 10,
        code_cards_count: int = 1,
        drop_table_code: str = "default",
        guild_id: int | None = None,
        crystal_price: int = 0,
        extra_luck_percent: float = 0.0,
    ) -> tuple[list[Card], list[Card]]:
        """Roll a fixed-size booster pack scoped to ``set_codes``."""
        codes = sorted({c for c in set_codes if c})
        if not codes:
            raise ValueError("set_codes must contain at least one set code.")
        if cards_count < 0 or code_cards_count < 0:
            raise ValueError("counts must be non-negative.")

        main_luck = await resolve_effective_rarity_luck(
            session,
            guild_id,
            extra_luck_percent=extra_luck_percent
            + pack_slot_luck_percent(crystal_price=crystal_price, code_slot=False),
        )
        code_luck = await resolve_effective_rarity_luck(
            session,
            guild_id,
            extra_luck_percent=extra_luck_percent
            + pack_slot_luck_percent(crystal_price=crystal_price, code_slot=True),
        )

        pulled: set[int] = set()
        regular: list[Card] = []
        for _ in range(int(cards_count)):
            card = await self._draw_pack_slot(
                session,
                set_codes=codes,
                drop_table_code=drop_table_code,
                pulled_ids=pulled,
                code_slot=False,
                luck_percent=main_luck,
            )
            regular.append(card)
            pulled.add(card.id)

        codes_list: list[Card] = []
        for _ in range(int(code_cards_count)):
            card = await self._draw_pack_slot(
                session,
                set_codes=codes,
                drop_table_code=drop_table_code,
                pulled_ids=pulled,
                code_slot=True,
                luck_percent=code_luck,
            )
            codes_list.append(card)
            pulled.add(card.id)

        return regular, codes_list

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
            from poke_pon_bot.services.card_roles import apply_new_instance_craft_uses

            apply_new_instance_craft_uses(row, card)
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
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
