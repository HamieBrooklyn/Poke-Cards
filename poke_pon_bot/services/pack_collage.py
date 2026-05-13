"""Build a single PNG collage of TCG card images for Discord (grid / side‑by‑side)."""

from __future__ import annotations

import asyncio
import io
from typing import Sequence

import httpx
from PIL import Image, ImageDraw, ImageFont

from poke_pon_bot.models.card import Card

TARGET_ROW_HEIGHT = 380
GAP = 14
BG = (32, 36, 42, 255)


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _placeholder(w: int, h: int, label: str) -> Image.Image:
    """Gray tile when a remote image fails to load."""
    img = Image.new("RGBA", (w, h), (55, 60, 68, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except OSError:
        font = None  # type: ignore[assignment]
    text = _truncate(label, 40)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((w - tw) // 2, (h - th) // 2), text, fill=(200, 200, 210), font=font)
    return img


def _resize_to_height(im: Image.Image, target_h: int) -> Image.Image:
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    w, h = im.size
    if h == 0:
        return im
    new_w = max(1, int(w * target_h / h))
    return im.resize((new_w, target_h), Image.Resampling.LANCZOS)


async def _fetch_art(client: httpx.AsyncClient, url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        im = Image.open(io.BytesIO(resp.content))
        return im.copy()
    except (httpx.HTTPError, OSError, ValueError):
        return None


def _compose_grid(tiles: Sequence[Image.Image], cols: int) -> Image.Image:
    """Place tiles row-wise in a grid; centers short rows horizontally."""
    n = len(tiles)
    if n == 0:
        raise ValueError("no tiles")
    cols = max(1, min(cols, n))
    rows: list[list[Image.Image]] = []
    for i in range(0, n, cols):
        rows.append(list(tiles[i : i + cols]))

    row_sizes: list[tuple[int, int]] = []
    for row in rows:
        rw = sum(im.width for im in row) + GAP * max(0, len(row) - 1)
        rh = max(im.height for im in row)
        row_sizes.append((rw, rh))

    canvas_w = max(w for w, _ in row_sizes)
    canvas_h = sum(h for _, h in row_sizes) + GAP * max(0, len(rows) - 1)

    canvas = Image.new("RGBA", (canvas_w, canvas_h), BG)
    y = 0
    for row, (_, rh) in zip(rows, row_sizes, strict=True):
        content_w = sum(im.width for im in row) + GAP * max(0, len(row) - 1)
        x = max(0, (canvas_w - content_w) // 2)
        for im in row:
            dy = y + max(0, (rh - im.height) // 2)
            if im.mode == "RGBA":
                canvas.paste(im, (x, dy), im)
            else:
                canvas.paste(im, (x, dy))
            x += im.width + GAP
        y += rh + GAP

    return canvas


def compose_vs_collage_with_gap_labels(tiles: Sequence[Image.Image], *, gap_labels: list[str]) -> Image.Image:
    """Compose exactly **two** tiles side-by-side with centered gap lettering."""
    if len(tiles) != 2:
        raise ValueError("expected exactly two tiles")
    if len(gap_labels) != 1:
        raise ValueError("expected exactly one gap label between tiles")

    a, b = tiles
    row_h = max(a.height, b.height)
    gap_px = max(34, min(72, int(min(a.width, b.width) * 0.10)))

    canvas_w = a.width + gap_px + b.width
    canvas_h = row_h
    canvas = Image.new("RGBA", (canvas_w, canvas_h), BG)

    dy_a = max(0, (row_h - a.height) // 2)
    dy_b = max(0, (row_h - b.height) // 2)

    if a.mode == "RGBA":
        canvas.paste(a, (0, dy_a), a)
    else:
        canvas.paste(a, (0, dy_a))

    ax_gap = a.width + gap_px // 2
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default()
    except OSError:
        font = None  # type: ignore[assignment]

    label = gap_labels[0]
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = int(ax_gap - tw / 2)
    ty = int((row_h - th) / 2)
    draw.text((tx, ty), label, fill=(235, 240, 255), font=font)

    x_b = a.width + gap_px
    if b.mode == "RGBA":
        canvas.paste(b, (x_b, dy_b), b)
    else:
        canvas.paste(b, (x_b, dy_b))

    return canvas


async def render_pack_collage_png(cards: list[Card]) -> io.BytesIO | None:
    """Download card art and return PNG bytes in a ``BytesIO``, or ``None`` on total failure."""
    if not cards:
        return None

    urls = [c.image_large_url or c.image_small_url for c in cards]
    async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": "Poke-Cards-Bot/1.0"}) as client:
        fetched = await asyncio.gather(*[_fetch_art(client, u) for u in urls])

    tiles: list[Image.Image] = []
    for card, raw in zip(cards, fetched, strict=True):
        if raw is None:
            tiles.append(_placeholder(260, TARGET_ROW_HEIGHT, card.name))
        else:
            tiles.append(_resize_to_height(raw, TARGET_ROW_HEIGHT))

    cols = 1 if len(tiles) == 1 else 2
    collage = _compose_grid(tiles, cols=cols)

    buf = io.BytesIO()
    collage.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


async def render_url_collage_png(urls: list[str], labels: list[str] | None = None) -> io.BytesIO | None:
    """Download images by URL and return a PNG collage (2 columns when possible).

    Used for duels where we only have image URLs, not `Card` rows.
    """
    if not urls:
        return None
    if labels is None:
        labels = ["" for _ in urls]
    if len(labels) != len(urls):
        raise ValueError("labels must match urls length")

    async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": "Poke-Cards-Bot/1.0"}) as client:
        fetched = await asyncio.gather(*[_fetch_art(client, u) for u in urls])

    tiles: list[Image.Image] = []
    for label, raw in zip(labels, fetched, strict=True):
        if raw is None:
            tiles.append(_placeholder(260, TARGET_ROW_HEIGHT, label or "Card"))
        else:
            tiles.append(_resize_to_height(raw, TARGET_ROW_HEIGHT))

    cols = 1 if len(tiles) == 1 else 2
    collage = _compose_grid(tiles, cols=cols)
    buf = io.BytesIO()
    collage.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


async def render_url_vs_collage_png(
    urls: list[str],
    labels: list[str] | None = None,
    *,
    gap_label: str = "VS",
) -> io.BytesIO | None:
    """Side-by-side collage with centered gap label (typically VS)."""
    if len(urls) != 2:
        raise ValueError("render_url_vs_collage_png expects exactly two URLs")
    if labels is None:
        labels = ["", ""]
    if len(labels) != 2:
        raise ValueError("labels must match urls length")

    async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": "Poke-Cards-Bot/1.0"}) as client:
        fetched = await asyncio.gather(*[_fetch_art(client, u) for u in urls])

    tiles: list[Image.Image] = []
    for label, raw in zip(labels, fetched, strict=True):
        if raw is None:
            tiles.append(_placeholder(260, TARGET_ROW_HEIGHT, label or "Card"))
        else:
            tiles.append(_resize_to_height(raw, TARGET_ROW_HEIGHT))

    collage = compose_vs_collage_with_gap_labels(tiles, gap_labels=[gap_label])
    buf = io.BytesIO()
    collage.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


async def render_single_card_png_from_url(url: str, *, label: str = "") -> io.BytesIO | None:
    """Download one card image and return PNG bytes resized similarly to collages."""
    if not url:
        return None
    async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": "Poke-Cards-Bot/1.0"}) as client:
        raw = await _fetch_art(client, url)
    if raw is None:
        tile = _placeholder(260, TARGET_ROW_HEIGHT, label or "Card")
    else:
        tile = _resize_to_height(raw, TARGET_ROW_HEIGHT)

    buf = io.BytesIO()
    tile.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
