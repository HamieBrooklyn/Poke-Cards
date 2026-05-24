"""Render a PSA-style graded slab PNG around a TCG card image."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import httpx
from PIL import Image, ImageDraw, ImageFont

from poke_pon_bot.services.grading import grade_label

if TYPE_CHECKING:
    from poke_pon_bot.models.card import Card

LABEL_H = 200
PAD = 28
FRAME = 14
BORDER_RED = (196, 30, 58, 255)
LABEL_BG = (252, 252, 252, 255)
FRAME_BG = (228, 232, 238, 255)
TEXT = (24, 24, 28, 255)
MUTED = (80, 86, 96, 255)
ACCENT = (196, 30, 58, 255)


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


def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    t = text
    while t and draw.textlength(t + ell, font=font) > max_w:
        t = t[:-1]
    return (t + ell) if t else ell


async def render_graded_slab_png(
    card: Card,
    *,
    grade: int,
    copy_index: int,
    total_copies: int,
    cert_suffix: str,
    client: httpx.AsyncClient | None = None,
) -> io.BytesIO | None:
    """Build a slab composite PNG; returns file-like buffer for Discord ``File``."""
    art_url = card.image_large_url or card.image_small_url
    if not art_url:
        return None

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=45.0)
    assert client is not None

    try:
        resp = await client.get(art_url, follow_redirects=True)
        resp.raise_for_status()
        art = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except (httpx.HTTPError, OSError, ValueError):
        if own_client:
            await client.aclose()
        return None

    card_h = 720
    w0, h0 = art.size
    if h0 > 0:
        card_w = max(1, int(w0 * card_h / h0))
        art = art.resize((card_w, card_h), Image.Resampling.LANCZOS)
    else:
        card_w, card_h = art.size

    inner_w = card_w + FRAME * 2
    canvas_w = inner_w + PAD * 2
    canvas_h = LABEL_H + card_h + FRAME * 2 + PAD * 2

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (18, 20, 24, 255))
    draw = ImageDraw.Draw(canvas)

    label_top = PAD
    label_box = (PAD, label_top, canvas_w - PAD, label_top + LABEL_H)
    draw.rounded_rectangle(label_box, radius=10, fill=LABEL_BG, outline=BORDER_RED, width=4)

    title_font = _font(22, bold=True)
    sub_font = _font(17)
    grade_font = _font(52, bold=True)
    small_font = _font(15)

    lx = PAD + 20
    rx = canvas_w - PAD - 20
    ty = label_top + 18

    line1 = _truncate(draw, (card.set_name or card.set_code or "POKÉMON").upper(), title_font, rx - lx)
    draw.text((lx, ty), line1, fill=TEXT, font=title_font)
    ty += 30

    name_line = card.name.upper()
    if card.tcg_rarity:
        name_line += f" — {card.tcg_rarity.upper()}"
    line2 = _truncate(draw, name_line, sub_font, rx - lx - 80)
    draw.text((lx, ty), line2, fill=TEXT, font=sub_font)
    ty += 26

    num = f"#{card.collector_number or '?'}"
    draw.text((lx, ty), num, fill=MUTED, font=small_font)

    gtxt = str(grade)
    glab = grade_label(grade)
    gw = draw.textlength(gtxt, font=grade_font)
    draw.text((rx - gw, label_top + 22), gtxt, fill=ACCENT, font=grade_font)
    lw = draw.textlength(glab, font=sub_font)
    draw.text((rx - lw, label_top + 88), glab, fill=MUTED, font=sub_font)

    cert = f"PP-{cert_suffix[:12].upper()}"
    cw = draw.textlength(cert, font=small_font)
    draw.text((rx - cw, label_top + LABEL_H - 34), cert, fill=MUTED, font=small_font)

    idx_txt = f"Copy #{copy_index:,} of {total_copies:,} globally"
    draw.text((lx, label_top + LABEL_H - 34), idx_txt, fill=MUTED, font=small_font)

    card_x = PAD + FRAME
    card_y = label_top + LABEL_H + FRAME
    frame_box = (PAD, label_top + LABEL_H, PAD + inner_w, label_top + LABEL_H + card_h + FRAME * 2)
    draw.rounded_rectangle(frame_box, radius=12, fill=FRAME_BG)

    canvas.paste(art, (card_x, card_y), art)

    gloss = Image.new("RGBA", (inner_w, card_h + FRAME), (255, 255, 255, 0))
    gdraw = ImageDraw.Draw(gloss)
    gdraw.polygon(
        [(0, 0), (inner_w, 0), (int(inner_w * 0.55), card_h + FRAME), (0, card_h + FRAME)],
        fill=(255, 255, 255, 55),
    )
    gdraw.rectangle((0, 0, inner_w, 40), fill=(255, 255, 255, 35))
    canvas.paste(gloss, (PAD, label_top + LABEL_H), gloss)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    out.seek(0)

    if own_client:
        await client.aclose()
    return out
