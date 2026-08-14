"""Live TCGPlayer quotes via pokemontcg.io, plus pokedollar investment lots."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_market import CardInvestment, CardMarketHistory, CardMarketQuote
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.wallet import InsufficientPokedollarsError, WalletService, format_pokedollars

_LOG = logging.getLogger(__name__)

TCG_CARD_URL = "https://api.pokemontcg.io/v2/cards/{card_id}"
QUOTE_TTL = timedelta(minutes=30)
HISTORY_DAYS_DEFAULT = 90
INVEST_MIN_PD = 100
INVEST_MAX_PD = 100_000
_PRICE_KEYS = (
    "holofoil",
    "reverseHolofoil",
    "normal",
    "1stEditionHolofoil",
    "unlimitedHolofoil",
    "1stEditionNormal",
    "unlimited",
)


class MarketUnavailableError(Exception):
    """No live TCGPlayer market for this printing."""


class InvestmentNotFoundError(Exception):
    """Position missing or not owned by the caller."""


class NotInCollectionError(Exception):
    """Caller does not own a copy of this catalog printing."""


@dataclass(frozen=True)
class MarketQuote:
    card_id: int
    usd_cents: int
    usd_low_cents: int | None
    usd_mid_cents: int | None
    usd_high_cents: int | None
    source: str
    tcgplayer_url: str | None
    fetched_at: datetime

    @property
    def usd(self) -> float:
        return self.usd_cents / 100.0


@dataclass(frozen=True)
class HistoryPoint:
    day: date
    usd_cents: int


@dataclass(frozen=True)
class PositionView:
    investment: CardInvestment
    card: Card
    quote: MarketQuote
    current_value: int
    pnl: int
    pnl_percent: float


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return max(1, int(round(n * 100)))


def _pick_tcgplayer_prices(payload: dict[str, Any]) -> tuple[int, int | None, int | None, int | None, str | None]:
    tcg = payload.get("tcgplayer") or {}
    url = tcg.get("url") if isinstance(tcg, dict) else None
    prices = tcg.get("prices") if isinstance(tcg, dict) else None
    if not isinstance(prices, dict):
        raise MarketUnavailableError("No TCGPlayer prices on this printing.")
    block: dict[str, Any] | None = None
    for key in _PRICE_KEYS:
        cand = prices.get(key)
        if isinstance(cand, dict) and _cents(cand.get("market")) is not None:
            block = cand
            break
    if block is None:
        for cand in prices.values():
            if isinstance(cand, dict) and _cents(cand.get("market")) is not None:
                block = cand
                break
    if block is None:
        raise MarketUnavailableError("No TCGPlayer market price for this printing.")
    market = _cents(block.get("market"))
    if market is None:
        raise MarketUnavailableError("No TCGPlayer market price for this printing.")
    return (
        market,
        _cents(block.get("low")),
        _cents(block.get("mid")),
        _cents(block.get("high")),
        str(url) if url else None,
    )


def position_current_value(*, pokedollars_in: int, entry_usd_cents: int, current_usd_cents: int) -> int:
    """Mark-to-market. Tracks live USD % change. Never negative."""
    if pokedollars_in <= 0:
        return 0
    if entry_usd_cents <= 0:
        return pokedollars_in
    raw = pokedollars_in * current_usd_cents / entry_usd_cents
    return max(0, int(round(raw)))


def format_usd_cents(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"${cents / 100.0:,.2f}"


def quote_from_row(card_id: int, row: CardMarketQuote) -> MarketQuote:
    return MarketQuote(
        card_id=int(card_id),
        usd_cents=int(row.usd_cents),
        usd_low_cents=row.usd_low_cents,
        usd_mid_cents=row.usd_mid_cents,
        usd_high_cents=row.usd_high_cents,
        source=row.source,
        tcgplayer_url=row.tcgplayer_url,
        fetched_at=_as_utc(row.fetched_at),
    )


@dataclass(frozen=True)
class MarketTrend:
    direction: str
    change_cents: int
    percent: float
    baseline_label: str
    current_cents: int
    baseline_cents: int

    @property
    def is_up(self) -> bool:
        return self.direction == "up"

    @property
    def is_down(self) -> bool:
        return self.direction == "down"

    def headline(self) -> str:
        if self.direction == "flat":
            return f"▬  FLAT vs {self.baseline_label}"
        arrow = "▲" if self.is_up else "▼"
        word = "UP" if self.is_up else "DOWN"
        return (
            f"{arrow}  {word}  {format_usd_cents(abs(self.change_cents))} "
            f"({self.percent:+.1f}%) vs {self.baseline_label}"
        )


def market_trend(
    quote: MarketQuote,
    history: list[HistoryPoint] | None = None,
) -> MarketTrend:
    """Prefer multi-day close movement; else market vs book mid."""
    hist = list(history or [])
    if history_has_movement(hist):
        baseline = hist[0].usd_cents
        current = hist[-1].usd_cents
        label = "first tracked day"
    else:
        current = quote.usd_cents
        _low, mid, _high = sane_book_cents(quote)
        if mid is None or mid <= 0:
            return MarketTrend("flat", 0, 0.0, "book mid", current, current)
        baseline = mid
        label = "book mid"
    change = current - baseline
    pct = (change / baseline * 100.0) if baseline else 0.0
    if change > 0:
        direction = "up"
    elif change < 0:
        direction = "down"
    else:
        direction = "flat"
    return MarketTrend(direction, change, pct, label, current, baseline)


def history_has_movement(points: list[HistoryPoint]) -> bool:
    if len(points) < 2:
        return False
    return len({p.usd_cents for p in points}) > 1


def sane_book_cents(quote: MarketQuote) -> tuple[int | None, int | None, int | None]:
    """Drop junk TCGPlayer highs (one $20 listing on a $0.10 common)."""
    market = quote.usd_cents
    low = quote.usd_low_cents
    mid = quote.usd_mid_cents
    high = quote.usd_high_cents
    ceiling = max(market, mid or 0, 1) * 4
    if high is not None and high > ceiling:
        high = None
    if low is not None and (low <= 0 or low > market):
        low = None
    return low, mid, high


async def load_quote(
    session: AsyncSession,
    card: Card,
    *,
    api_key: str | None,
    force: bool = False,
    allow_stale: bool = False,
) -> MarketQuote:
    """Cache-first quote. Browse/flip should pass ``allow_stale=True`` so we never block on pokemontcg.io."""
    existing = await session.get(CardMarketQuote, card.id)
    if existing is not None and not force:
        age = _utc_now() - _as_utc(existing.fetched_at)
        if age < QUOTE_TTL or allow_stale:
            return quote_from_row(int(card.id), existing)
    return await refresh_quote(session, card, api_key=api_key, force=force)


async def _fetch_remote_quote(
    card: Card,
    *,
    api_key: str | None,
) -> tuple[int, int | None, int | None, int | None, str | None]:
    headers = {"User-Agent": "PokePon/market"}
    if api_key:
        headers["X-Api-Key"] = api_key
    url = TCG_CARD_URL.format(card_id=card.tcg_card_id)
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers, follow_redirects=True)
        if resp.status_code == 404:
            raise MarketUnavailableError("Printing not found on pokemontcg.io.")
        resp.raise_for_status()
        body = resp.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise MarketUnavailableError("Unexpected pokemontcg.io response.")
    return _pick_tcgplayer_prices(data)


async def _upsert_history(session: AsyncSession, card_id: int, usd_cents: int) -> None:
    today = _utc_now().date()
    row = (
        await session.execute(
            select(CardMarketHistory).where(
                CardMarketHistory.card_id == card_id,
                CardMarketHistory.day == today,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(CardMarketHistory(card_id=card_id, day=today, usd_cents=usd_cents))
        return
    row.usd_cents = usd_cents


async def refresh_quote(
    session: AsyncSession,
    card: Card,
    *,
    api_key: str | None,
    force: bool = False,
) -> MarketQuote:
    existing = await session.get(CardMarketQuote, card.id)
    now = _utc_now()
    if (
        existing is not None
        and not force
        and now - _as_utc(existing.fetched_at) < QUOTE_TTL
    ):
        return quote_from_row(int(card.id), existing)
    try:
        market, low, mid, high, url = await _fetch_remote_quote(card, api_key=api_key)
    except (httpx.HTTPError, MarketUnavailableError):
        if existing is not None:
            _LOG.warning("market quote fetch failed card=%s; using cache", card.id)
            return quote_from_row(int(card.id), existing)
        raise
    if existing is None:
        existing = CardMarketQuote(card_id=int(card.id), usd_cents=market, source="tcgplayer")
        session.add(existing)
    existing.usd_cents = market
    existing.usd_low_cents = low
    existing.usd_mid_cents = mid
    existing.usd_high_cents = high
    existing.source = "tcgplayer"
    existing.tcgplayer_url = url
    existing.fetched_at = now
    await _upsert_history(session, int(card.id), market)
    await session.flush()
    return MarketQuote(
        card_id=int(card.id),
        usd_cents=market,
        usd_low_cents=low,
        usd_mid_cents=mid,
        usd_high_cents=high,
        source="tcgplayer",
        tcgplayer_url=url,
        fetched_at=now,
    )


async def load_history(
    session: AsyncSession,
    card_id: int,
    *,
    days: int = HISTORY_DAYS_DEFAULT,
) -> list[HistoryPoint]:
    cutoff = _utc_now().date() - timedelta(days=max(1, days))
    rows = (
        await session.execute(
            select(CardMarketHistory)
            .where(CardMarketHistory.card_id == card_id, CardMarketHistory.day >= cutoff)
            .order_by(CardMarketHistory.day.asc())
        )
    ).scalars().all()
    return [HistoryPoint(day=r.day, usd_cents=int(r.usd_cents)) for r in rows]


def _position_view(inv: CardInvestment, card: Card, quote: MarketQuote) -> PositionView:
    current = position_current_value(
        pokedollars_in=int(inv.pokedollars_in),
        entry_usd_cents=int(inv.entry_usd_cents),
        current_usd_cents=quote.usd_cents,
    )
    pnl = current - int(inv.pokedollars_in)
    pct = (pnl / inv.pokedollars_in * 100.0) if inv.pokedollars_in else 0.0
    return PositionView(
        investment=inv,
        card=card,
        quote=quote,
        current_value=current,
        pnl=pnl,
        pnl_percent=pct,
    )


async def user_owns_catalog_card(
    session: AsyncSession,
    *,
    discord_user_id: int,
    card_id: int,
) -> bool:
    row = (
        await session.execute(
            select(UserCardInstance.id)
            .where(
                UserCardInstance.discord_user_id == discord_user_id,
                UserCardInstance.card_id == card_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def get_open_position(
    session: AsyncSession,
    *,
    discord_user_id: int,
    card_id: int,
) -> CardInvestment | None:
    return (
        await session.execute(
            select(CardInvestment).where(
                CardInvestment.discord_user_id == discord_user_id,
                CardInvestment.card_id == card_id,
            )
        )
    ).scalar_one_or_none()


async def buy_position(
    session: AsyncSession,
    *,
    discord_user_id: int,
    card: Card,
    amount: int,
    api_key: str | None,
    wallet: WalletService | None = None,
) -> PositionView:
    if amount < INVEST_MIN_PD:
        raise ValueError(f"Minimum invest is {format_pokedollars(INVEST_MIN_PD)}.")
    if amount > INVEST_MAX_PD:
        raise ValueError(f"Maximum invest is {format_pokedollars(INVEST_MAX_PD)} per buy.")
    if not await user_owns_catalog_card(
        session, discord_user_id=discord_user_id, card_id=int(card.id)
    ):
        raise NotInCollectionError
    quote = await refresh_quote(session, card, api_key=api_key)
    wallet = wallet or WalletService()
    try:
        await wallet.try_debit(session, discord_user_id, amount)
    except InsufficientPokedollarsError:
        raise
    existing = (
        await session.execute(
            select(CardInvestment).where(
                CardInvestment.discord_user_id == discord_user_id,
                CardInvestment.card_id == card.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = CardInvestment(
            discord_user_id=discord_user_id,
            card_id=int(card.id),
            pokedollars_in=amount,
            entry_usd_cents=quote.usd_cents,
            opened_at=_utc_now(),
        )
        session.add(existing)
    else:
        old_in = int(existing.pokedollars_in)
        old_entry = int(existing.entry_usd_cents)
        new_in = old_in + amount
        existing.entry_usd_cents = max(
            1,
            int(round((old_entry * old_in + quote.usd_cents * amount) / new_in)),
        )
        existing.pokedollars_in = new_in
    await session.flush()
    return _position_view(existing, card, quote)


async def sell_position(
    session: AsyncSession,
    *,
    discord_user_id: int,
    investment_id: int,
    api_key: str | None,
    wallet: WalletService | None = None,
) -> tuple[PositionView, int]:
    inv = await session.get(CardInvestment, investment_id)
    if inv is None or int(inv.discord_user_id) != int(discord_user_id):
        raise InvestmentNotFoundError
    card = await session.get(Card, inv.card_id)
    if card is None:
        raise InvestmentNotFoundError
    quote = await refresh_quote(session, card, api_key=api_key)
    view = _position_view(inv, card, quote)
    wallet = wallet or WalletService()
    new_bal = await wallet.try_credit(session, discord_user_id, view.current_value)
    await session.delete(inv)
    await session.flush()
    return view, new_bal


async def list_positions(
    session: AsyncSession,
    *,
    discord_user_id: int,
    api_key: str | None,
) -> list[PositionView]:
    rows = (
        await session.execute(
            select(CardInvestment, Card)
            .join(Card, Card.id == CardInvestment.card_id)
            .where(CardInvestment.discord_user_id == discord_user_id)
            .order_by(CardInvestment.opened_at.desc())
        )
    ).all()
    out: list[PositionView] = []
    for inv, card in rows:
        try:
            quote = await load_quote(session, card, api_key=api_key, allow_stale=True)
        except (httpx.HTTPError, MarketUnavailableError):
            continue
        out.append(_position_view(inv, card, quote))
    return out


async def load_position(
    session: AsyncSession,
    *,
    discord_user_id: int,
    investment_id: int,
    api_key: str | None,
) -> PositionView:
    inv = await session.get(CardInvestment, investment_id)
    if inv is None or int(inv.discord_user_id) != int(discord_user_id):
        raise InvestmentNotFoundError
    card = await session.get(Card, inv.card_id)
    if card is None:
        raise InvestmentNotFoundError
    quote = await load_quote(session, card, api_key=api_key, allow_stale=True)
    return _position_view(inv, card, quote)


async def invested_card_ids(session: AsyncSession) -> list[int]:
    rows = (await session.execute(select(CardInvestment.card_id).distinct())).scalars().all()
    return [int(x) for x in rows]


def quote_payload(quote: MarketQuote) -> dict[str, Any]:
    return {
        "usd": quote.usd,
        "usd_cents": quote.usd_cents,
        "usd_low": None if quote.usd_low_cents is None else quote.usd_low_cents / 100.0,
        "usd_mid": None if quote.usd_mid_cents is None else quote.usd_mid_cents / 100.0,
        "usd_high": None if quote.usd_high_cents is None else quote.usd_high_cents / 100.0,
        "source": quote.source,
        "tcgplayer_url": quote.tcgplayer_url,
        "fetched_at": quote.fetched_at.isoformat(),
    }


def position_payload(view: PositionView) -> dict[str, Any]:
    inv = view.investment
    card = view.card
    return {
        "id": int(inv.id),
        "pokedollars_in": int(inv.pokedollars_in),
        "entry_usd": inv.entry_usd_cents / 100.0,
        "current_value": view.current_value,
        "pnl": view.pnl,
        "pnl_percent": round(view.pnl_percent, 2),
        "opened_at": _as_utc(inv.opened_at).isoformat(),
        "quote": quote_payload(view.quote),
        "card": {
            "id": int(card.id),
            "tcg_card_id": card.tcg_card_id,
            "name": card.name,
            "set_code": card.set_code,
            "set_name": card.set_name,
            "collector_number": card.collector_number,
            "image_small_url": card.image_small_url,
            "image_large_url": card.image_large_url,
            "tcg_rarity": card.tcg_rarity,
        },
    }
