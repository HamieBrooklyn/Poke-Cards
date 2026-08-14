"""PNG sparkline of TCGPlayer USD history (or today's book) for Discord embeds."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from poke_pon_bot.services.card_market import (
    HistoryPoint,
    MarketQuote,
    MarketTrend,
    format_usd_cents,
    history_has_movement,
    market_trend,
    sane_book_cents,
)

W, H = 920, 420
PAD_L, PAD_R, PAD_T, PAD_B = 72, 28, 48, 48
BG = (18, 20, 26, 255)
GRID = (42, 48, 60, 255)
UP = (80, 210, 140, 255)
DOWN = (230, 90, 110, 255)
FLAT = (180, 190, 210, 255)
TEXT = (230, 232, 238, 255)
MUTED = (140, 148, 162, 255)
FILL_UP = (80, 210, 140, 48)
FILL_DOWN = (230, 90, 110, 48)
BOOK = (91, 140, 255, 255)
BOOK_FILL = (91, 140, 255, 40)
MID = (250, 204, 21, 255)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else None,
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "arial.ttf",
    )
    for p in paths:
        if not p:
            continue
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _axis_range(values: list[int]) -> tuple[int, int]:
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        hi = lo + 100
    pad = max(25, int((hi - lo) * 0.12))
    return max(0, lo - pad), hi + pad


def render_market_chart_png(
    points: list[HistoryPoint],
    *,
    title: str,
    current_cents: int,
    quote: MarketQuote | None = None,
) -> io.BytesIO:
    trend = market_trend(quote, points) if quote is not None else None
    tint = (16, 36, 28, 255) if trend and trend.is_up else (40, 18, 22, 255) if trend and trend.is_down else BG
    img = Image.new("RGBA", (W, H), tint)
    draw = ImageDraw.Draw(img)
    title_font = _font(20, bold=True)
    badge_font = _font(22, bold=True)
    axis_font = _font(13)
    draw.text((PAD_L, 10), title[:48], fill=TEXT, font=title_font)
    if trend is not None:
        badge = trend.headline()
        color = UP if trend.is_up else DOWN if trend.is_down else FLAT
        bw = draw.textlength(badge, font=badge_font)
        draw.text((W - PAD_R - bw, 10), badge, fill=color, font=badge_font)

    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    if history_has_movement(points):
        _draw_series(draw, points, axis_font, plot_w, plot_h, trend)
    else:
        _draw_book(draw, quote, current_cents, axis_font, plot_w, plot_h, trend)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    out.seek(0)
    return out


def _draw_series(
    draw: ImageDraw.ImageDraw,
    series: list[HistoryPoint],
    axis_font: ImageFont.ImageFont,
    plot_w: int,
    plot_h: int,
    trend: MarketTrend | None = None,
) -> None:
    lo, hi = _axis_range([p.usd_cents for p in series])
    n = len(series)

    def xy(i: int, cents: int) -> tuple[int, int]:
        x = PAD_L + int(i * plot_w / max(1, n - 1))
        y = PAD_T + int((hi - cents) * plot_h / (hi - lo))
        return x, y

    for g in range(5):
        gy = PAD_T + int(g * plot_h / 4)
        draw.line((PAD_L, gy, W - PAD_R, gy), fill=GRID, width=1)
        val = hi - int(g * (hi - lo) / 4)
        draw.text((10, gy - 8), format_usd_cents(val), fill=MUTED, font=axis_font)

    coords = [xy(i, p.usd_cents) for i, p in enumerate(series)]
    first, last = series[0].usd_cents, series[-1].usd_cents
    color = UP if last > first else DOWN if last < first else FLAT
    fill = FILL_UP if last > first else FILL_DOWN if last < first else (180, 190, 210, 36)
    poly = coords + [(coords[-1][0], PAD_T + plot_h), (coords[0][0], PAD_T + plot_h)]
    draw.polygon(poly, fill=fill)
    y0 = xy(0, first)[1]
    draw.line((PAD_L, y0, W - PAD_R, y0), fill=(*color[:3], 90), width=2)
    draw.line(coords, fill=color, width=5)
    lx, ly = coords[-1]
    draw.polygon([(lx - 2, ly), (lx + 16, ly - 11), (lx + 16, ly + 11)], fill=color)
    draw.ellipse((lx - 7, ly - 7, lx + 7, ly + 7), fill=color)
    draw.text((PAD_L, H - 32), series[0].day.isoformat(), fill=MUTED, font=axis_font)
    last_label = series[-1].day.isoformat()
    lw = draw.textlength(last_label, font=axis_font)
    draw.text((W - PAD_R - lw, H - 32), last_label, fill=MUTED, font=axis_font)


def _draw_book(
    draw: ImageDraw.ImageDraw,
    quote: MarketQuote | None,
    current_cents: int,
    axis_font: ImageFont.ImageFont,
    plot_w: int,
    plot_h: int,
    trend: MarketTrend | None = None,
) -> None:
    low = mid = high = None
    market = current_cents
    if quote is not None:
        low, mid, high = sane_book_cents(quote)
        market = quote.usd_cents
    values = [v for v in (low, mid, high, market) if v is not None]
    if not values:
        values = [current_cents or 100]
    lo, hi = _axis_range(values)

    def y_of(cents: int) -> int:
        return PAD_T + int((hi - cents) * plot_h / (hi - lo))

    for g in range(5):
        gy = PAD_T + int(g * plot_h / 4)
        draw.line((PAD_L, gy, W - PAD_R, gy), fill=GRID, width=1)
        val = hi - int(g * (hi - lo) / 4)
        draw.text((10, gy - 8), format_usd_cents(val), fill=MUTED, font=axis_font)

    x0 = PAD_L + int(plot_w * 0.22)
    x1 = PAD_L + int(plot_w * 0.78)
    band_lo = low if low is not None else market
    band_hi = high if high is not None else (mid if mid is not None else market)
    y_hi = y_of(max(band_lo, band_hi))
    y_lo = y_of(min(band_lo, band_hi))
    if y_lo - y_hi < 8:
        y_hi -= 12
        y_lo += 12
    draw.rounded_rectangle((x0, y_hi, x1, y_lo), radius=12, fill=BOOK_FILL, outline=BOOK, width=2)

    if mid is not None:
        ym = y_of(mid)
        draw.line((x0, ym, x1, ym), fill=MID, width=2)
        draw.text((x1 + 8, ym - 8), f"Mid {format_usd_cents(mid)}", fill=MID, font=axis_font)

    ymkt = y_of(market)
    mcolor = UP if trend and trend.is_up else DOWN if trend and trend.is_down else FLAT
    draw.line((PAD_L, ymkt, W - PAD_R, ymkt), fill=mcolor, width=5)
    draw.ellipse((PAD_L + plot_w // 2 - 8, ymkt - 8, PAD_L + plot_w // 2 + 8, ymkt + 8), fill=mcolor)
    draw.text(
        (PAD_L + 8, PAD_T + plot_h + 8),
        f"Market {format_usd_cents(market)}"
        + (f"  ·  Low {format_usd_cents(low)}" if low is not None else "")
        + (f"  ·  High {format_usd_cents(high)}" if high is not None else ""),
        fill=MUTED,
        font=axis_font,
    )
