"""Booster pack lifecycle: purchase -> open -> scrap.

Packs are owned :class:`UserPackInstance` rows. Opening rolls 10 + 1 :class:`UserCardInstance`
rows scoped to the series's ``card_series_sets`` and credits 1 Crystal per code card. The
flip view in :mod:`poke_pon_bot.cogs.packs` lets the user scrap unwanted instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_series import CardSeries, CardSeriesSet
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.pack_instance import UserPackInstance
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.services.instance_public_id import new_public_id
from poke_pon_bot.services.wallet import WalletService

DEFAULT_RANDOM_PACK_PRICE = 5_000
DEFAULT_RANDOM_PACK_CRYSTAL_PRICE = 10


class NoActiveSeriesError(LookupError):
    """No ``card_series`` row has ``is_active=True`` — random/SKU purchase has no candidates."""


class UnknownSeriesError(LookupError):
    """The supplied ``series_id`` / ``code`` does not exist."""


class PackNotFoundError(LookupError):
    """The pack instance does not exist or is not owned by ``owner_id``."""


class PackAlreadyOpenedError(RuntimeError):
    """The caller tried to open a pack whose ``opened_at`` is set."""


class PackEmptyError(RuntimeError):
    """The series resolved to zero set codes — pack cannot roll cards."""


class CardNotInOpenedPackError(LookupError):
    """The instance is not eligible for scrapping (not owned, or not from a pack open)."""


@dataclass(frozen=True)
class OpenedPackResult:
    """The freshly opened pack — ``regular`` and ``code_cards`` already saved as instances."""

    pack_instance_id: int
    pack_public_id: str
    series_id: int
    regular: list[tuple[UserCardInstance, Card]]
    code_cards: list[tuple[UserCardInstance, Card]]
    crystals_credited: int


class PackService:
    """High-level orchestrator over :class:`WalletService`, :class:`CrystalsService`,
    :class:`DropService` and the new pack/series tables."""

    def __init__(
        self,
        *,
        wallet: WalletService | None = None,
        crystals: CrystalsService | None = None,
    ) -> None:
        self._wallet = wallet or WalletService()
        self._crystals = crystals or CrystalsService()

    async def list_active_series(self, session: AsyncSession) -> list[CardSeries]:
        rows = await session.execute(
            select(CardSeries).where(CardSeries.is_active.is_(True)).order_by(CardSeries.id)
        )
        return list(rows.scalars())

    async def get_series_by_code(
        self, session: AsyncSession, code: str
    ) -> CardSeries | None:
        return await session.scalar(
            select(CardSeries).where(CardSeries.code == code.strip())
        )

    async def _series_set_codes(
        self, session: AsyncSession, series_id: int
    ) -> list[str]:
        rows = await session.execute(
            select(CardSeriesSet.set_code).where(CardSeriesSet.series_id == series_id)
        )
        return sorted({r[0] for r in rows.all()})

    async def _new_pack_instance(
        self,
        session: AsyncSession,
        *,
        discord_user_id: int,
        series_id: int,
        source: str,
        guild_id: int | None = None,
    ) -> UserPackInstance:
        """Insert a fresh unopened pack, retrying on the (extremely rare) public_id clash."""
        for _ in range(12):
            row = UserPackInstance(
                public_id=new_public_id(),
                discord_user_id=int(discord_user_id),
                guild_id=int(guild_id) if guild_id is not None else None,
                series_id=int(series_id),
                source=source,
            )
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                continue
            return row
        msg = "Could not assign a unique Pack ID — try again."
        raise RuntimeError(msg)

    async def _pick_random_active_series_id(
        self, session: AsyncSession, drop_service: DropService
    ) -> int:
        active = await self.list_active_series(session)
        if not active:
            raise NoActiveSeriesError("No active card_series rows; check pack_series.v1.yaml.")
        return drop_service._rng.choice(active).id  # noqa: SLF001 — reuse the seeded RNG

    async def purchase_with_pokedollars(
        self,
        session: AsyncSession,
        drop_service: DropService,
        *,
        discord_user_id: int,
        price: int = DEFAULT_RANDOM_PACK_PRICE,
        guild_id: int | None = None,
    ) -> UserPackInstance:
        """Random active series. Debits Pokedollars first; raises ``InsufficientPokedollarsError`` on overdraft."""
        await self._wallet.try_debit(session, discord_user_id, int(price))
        series_id = await self._pick_random_active_series_id(session, drop_service)
        return await self._new_pack_instance(
            session,
            discord_user_id=discord_user_id,
            series_id=series_id,
            source="purchase_pokedollars",
            guild_id=guild_id,
        )

    async def purchase_random_with_crystals(
        self,
        session: AsyncSession,
        drop_service: DropService,
        *,
        discord_user_id: int,
        crystal_price: int = DEFAULT_RANDOM_PACK_CRYSTAL_PRICE,
        guild_id: int | None = None,
    ) -> UserPackInstance:
        """Random active series, paid in Crystals. Mirrors the Pokedollar path but uses
        :class:`CrystalsService` and a fixed flat price (independent of the rolled
        series' per-pack ``crystal_price``)."""
        await self._crystals.try_debit(session, discord_user_id, int(crystal_price))
        series_id = await self._pick_random_active_series_id(session, drop_service)
        return await self._new_pack_instance(
            session,
            discord_user_id=discord_user_id,
            series_id=series_id,
            source="purchase_crystals_random",
            guild_id=guild_id,
        )

    async def purchase_with_crystals(
        self,
        session: AsyncSession,
        *,
        discord_user_id: int,
        series_id: int,
        guild_id: int | None = None,
    ) -> UserPackInstance:
        """Specific series chosen by the user. Debits ``series.crystal_price`` Crystals."""
        series = await session.get(CardSeries, int(series_id))
        if series is None or not series.is_active:
            raise UnknownSeriesError(f"series_id={series_id} not found or inactive.")
        await self._crystals.try_debit(session, discord_user_id, int(series.crystal_price))
        return await self._new_pack_instance(
            session,
            discord_user_id=discord_user_id,
            series_id=series.id,
            source="purchase_crystals",
            guild_id=guild_id,
        )

    async def grant_consumable_pack(
        self,
        session: AsyncSession,
        drop_service: DropService,
        *,
        discord_user_id: int,
        series_id: int | None = None,
        guild_id: int | None = None,
    ) -> UserPackInstance:
        """No debit (paid via Discord SKU).

        If ``series_id`` is supplied, grant that exact active series; otherwise pick a random
        active series. The explicit series path is used when a user buys from a `/packv` pack
        detail page.
        """
        if series_id is None:
            series_id = await self._pick_random_active_series_id(session, drop_service)
        else:
            series = await session.get(CardSeries, int(series_id))
            if series is None or not series.is_active:
                raise UnknownSeriesError(f"series_id={series_id} not found or inactive.")
        return await self._new_pack_instance(
            session,
            discord_user_id=discord_user_id,
            series_id=int(series_id),
            source="sku_consumable",
            guild_id=guild_id,
        )

    async def grant_dev_pack(
        self,
        session: AsyncSession,
        drop_service: DropService,
        *,
        discord_user_id: int,
        series_id: int | None = None,
        guild_id: int | None = None,
    ) -> UserPackInstance:
        """Free-from-thin-air pack for QA. ``series_id`` falls back to a random active series."""
        chosen = (
            int(series_id)
            if series_id is not None
            else await self._pick_random_active_series_id(session, drop_service)
        )
        series = await session.get(CardSeries, chosen)
        if series is None:
            raise UnknownSeriesError(f"series_id={chosen} not found.")
        return await self._new_pack_instance(
            session,
            discord_user_id=discord_user_id,
            series_id=series.id,
            source="dev",
            guild_id=guild_id,
        )

    async def open_pack(
        self,
        session: AsyncSession,
        drop_service: DropService,
        *,
        pack_instance_id: int,
        owner_id: int,
    ) -> OpenedPackResult:
        """Roll cards, eagerly persist them, mark the pack as opened, credit crystals.

        Cards are saved **before** the flip view appears so a crash mid-flip doesn't lose the
        pull. ``scrap_card`` is the only way to remove an instance after open.
        """
        pack = await session.get(UserPackInstance, int(pack_instance_id))
        if pack is None or pack.discord_user_id != int(owner_id):
            raise PackNotFoundError(
                f"Pack {pack_instance_id} is not owned by user {owner_id}."
            )
        if pack.opened_at is not None:
            raise PackAlreadyOpenedError(f"Pack {pack_instance_id} is already opened.")

        series = await session.get(CardSeries, pack.series_id)
        if series is None:
            raise UnknownSeriesError(f"Pack {pack_instance_id} references missing series.")

        set_codes = await self._series_set_codes(session, series.id)
        if not set_codes:
            raise PackEmptyError(
                f"Series {series.code!r} has no `card_series_sets` rows — edit "
                "config/pack_series.v1.yaml and restart the bot."
            )

        regular_cards, code_cards = await drop_service.roll_pack_for_series(
            session,
            set_codes=set_codes,
            cards_count=int(series.cards_per_pack),
            code_cards_count=int(series.code_cards_per_pack),
        )

        regular_pairs: list[tuple[UserCardInstance, Card]] = []
        for card in regular_cards:
            inst = await self._save_owned_instance(
                session, discord_user_id=owner_id, card=card, source="pack_open"
            )
            regular_pairs.append((inst, card))

        code_pairs: list[tuple[UserCardInstance, Card]] = []
        for card in code_cards:
            inst = await self._save_owned_instance(
                session, discord_user_id=owner_id, card=card, source="pack_code_card"
            )
            code_pairs.append((inst, card))

        pack.opened_at = datetime.now(UTC)

        crystals = int(series.code_cards_per_pack)
        if crystals > 0:
            await self._crystals.try_credit(session, owner_id, crystals)

        return OpenedPackResult(
            pack_instance_id=pack.id,
            pack_public_id=pack.public_id,
            series_id=series.id,
            regular=regular_pairs,
            code_cards=code_pairs,
            crystals_credited=crystals,
        )

    async def _save_owned_instance(
        self,
        session: AsyncSession,
        *,
        discord_user_id: int,
        card: Card,
        source: str,
    ) -> UserCardInstance:
        for _ in range(12):
            inst = UserCardInstance(
                public_id=new_public_id(),
                discord_user_id=int(discord_user_id),
                card_id=card.id,
                source=source,
            )
            try:
                async with session.begin_nested():
                    session.add(inst)
                    await session.flush()
            except IntegrityError:
                continue
            return inst
        msg = "Could not assign a unique Card ID — try again."
        raise RuntimeError(msg)

    async def scrap_card(
        self,
        session: AsyncSession,
        *,
        instance_id: int,
        owner_id: int,
    ) -> None:
        """Permanent delete (no Pokedollar refund). Owner-gated."""
        inst = await session.get(UserCardInstance, int(instance_id))
        if inst is None or inst.discord_user_id != int(owner_id):
            raise CardNotInOpenedPackError(
                f"Card {instance_id} is not owned by user {owner_id}."
            )
        await session.execute(
            delete(UserCardInstance).where(UserCardInstance.id == inst.id)
        )

    async def list_unopened_for_user(
        self,
        session: AsyncSession,
        *,
        discord_user_id: int,
        series_id: int | None = None,
    ) -> list[UserPackInstance]:
        stmt = (
            select(UserPackInstance)
            .where(UserPackInstance.discord_user_id == int(discord_user_id))
            .where(UserPackInstance.opened_at.is_(None))
            .order_by(UserPackInstance.obtained_at.desc(), UserPackInstance.id.desc())
        )
        if series_id is not None:
            stmt = stmt.where(UserPackInstance.series_id == int(series_id))
        rows = await session.execute(stmt)
        return list(rows.scalars())

    async def get_pack_by_public_id(
        self,
        session: AsyncSession,
        public_id: str,
        owner_id: int,
    ) -> UserPackInstance | None:
        from poke_pon_bot.services.instance_public_id import normalize_public_id

        n = normalize_public_id(public_id)
        if n is None:
            return None
        return await session.scalar(
            select(UserPackInstance).where(
                UserPackInstance.discord_user_id == int(owner_id),
                UserPackInstance.public_id == n,
            )
        )

    async def opened_counts_by_series(
        self,
        session: AsyncSession,
        *,
        guild_id: int | None = None,
    ) -> dict[int, int]:
        """``{series_id: opened_count}`` for ``/packcat sort:popular``.

        When ``guild_id`` is supplied, only counts packs bought in that guild — DM-bought
        packs (``guild_id IS NULL``) are excluded from server-scoped tallies.
        """
        stmt = (
            select(UserPackInstance.series_id, func.count(UserPackInstance.id))
            .where(UserPackInstance.opened_at.is_not(None))
            .group_by(UserPackInstance.series_id)
        )
        if guild_id is not None:
            stmt = stmt.where(UserPackInstance.guild_id == int(guild_id))
        rows = await session.execute(stmt)
        return {int(sid): int(n) for sid, n in rows.all() if sid is not None}

    async def top_rarity_by_series(
        self,
        session: AsyncSession,
    ) -> dict[int, int]:
        """``{series_id: max(rarity_class_id)}`` across each series' set codes.

        Used by ``/packcat sort:rarest`` to rank series by the rarest possible pull. A
        series with no catalog cards (e.g. an out-of-sync set) is omitted from the map.
        """
        stmt = (
            select(
                CardSeriesSet.series_id,
                func.max(Card.rarity_class_id),
            )
            .join(Card, Card.set_code == CardSeriesSet.set_code)
            .group_by(CardSeriesSet.series_id)
        )
        rows = await session.execute(stmt)
        return {int(sid): int(top) for sid, top in rows.all() if top is not None}
