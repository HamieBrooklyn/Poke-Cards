"""Mark-to-market math for card investments."""

from datetime import UTC, date, datetime

from poke_pon_bot.services.card_market import (
    HistoryPoint,
    MarketQuote,
    history_has_movement,
    position_current_value,
    sane_book_cents,
)


def test_price_up_pays_more() -> None:
    assert position_current_value(pokedollars_in=1000, entry_usd_cents=1000, current_usd_cents=1200) == 1200


def test_price_down_loses() -> None:
    assert position_current_value(pokedollars_in=1000, entry_usd_cents=1000, current_usd_cents=500) == 500


def test_flat_returns_principal() -> None:
    assert position_current_value(pokedollars_in=2500, entry_usd_cents=399, current_usd_cents=399) == 2500


def test_zero_entry_keeps_principal() -> None:
    assert position_current_value(pokedollars_in=800, entry_usd_cents=0, current_usd_cents=100) == 800


def _quote(**kwargs) -> MarketQuote:
    base = dict(
        card_id=1,
        usd_cents=1000,
        usd_low_cents=800,
        usd_mid_cents=1100,
        usd_high_cents=1400,
        source="tcgplayer",
        tcgplayer_url=None,
        fetched_at=datetime.now(UTC),
    )
    base.update(kwargs)
    return MarketQuote(**base)


def test_sane_book_drops_junk_high() -> None:
    low, mid, high = sane_book_cents(_quote(usd_cents=10, usd_low_cents=1, usd_mid_cents=15, usd_high_cents=2000))
    assert low == 1
    assert mid == 15
    assert high is None


def test_history_needs_two_different_closes() -> None:
    assert not history_has_movement([HistoryPoint(day=date(2026, 8, 14), usd_cents=1000)])
    assert not history_has_movement(
        [
            HistoryPoint(day=date(2026, 8, 13), usd_cents=1000),
            HistoryPoint(day=date(2026, 8, 14), usd_cents=1000),
        ]
    )
    assert history_has_movement(
        [
            HistoryPoint(day=date(2026, 8, 13), usd_cents=1000),
            HistoryPoint(day=date(2026, 8, 14), usd_cents=1200),
        ]
    )
