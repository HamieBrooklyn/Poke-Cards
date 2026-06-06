"""Import promotional / out-of-API cards from YAML + bundled static art."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.rarity_normalize import assert_valid_code

_LOG = logging.getLogger(__name__)

_ANCIENT_MEW_ATTACKS = [
    {
        "name": "Psyche",
        "damage": "40",
        "text": None,
        "cost": ["Psychic", "Psychic"],
    }
]

_SET_CODE = "promo"
_SET_NAME = "Miscellaneous Promotional Cards"
_SUPERTYPE = "Pokémon"
_HP = "30"
_DEX = [151]
_TYPES = ["Psychic"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def manual_card_static_path(art: str, *, large: bool) -> str:
    """URL path served by the bot web app (prefix with ``web_public_url`` for browsers)."""
    size = "large" if large else "small"
    return f"/static/manual-cards/ancient-mew/{art}-{size}.jpg"


def resolve_public_image_url(raw: str, web_public_url: str | None) -> str:
    """Turn ``/static/...`` paths into absolute HTTPS URLs for browsers and Discord."""
    if not raw.startswith("/static/"):
        return raw
    base = (web_public_url or "").strip().rstrip("/")
    if not base:
        import os

        base = (os.environ.get("WEB_PUBLIC_URL") or "https://api.pokepon.org").strip().rstrip(
            "/"
        )
    return f"{base}{raw}"


async def _rarity_id(session: AsyncSession, code: str) -> int:
    assert_valid_code(code)
    row = await session.scalar(select(RarityClass.id).where(RarityClass.code == code))
    if row is None:
        msg = f"Missing rarity_classes row for code {code!r}"
        raise RuntimeError(msg)
    return int(row)


def load_ancient_mew_yaml(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or (_repo_root() / "config" / "manual_cards" / "ancient_mew.yaml")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cards = data.get("cards") or []
    if not isinstance(cards, list) or not cards:
        raise ValueError(f"{p}: expected non-empty `cards:` list")
    return cards


async def upsert_ancient_mew_cards(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    web_public_url: str | None = None,
) -> int:
    """Insert or update all Ancient Mew variants from YAML."""
    specs = load_ancient_mew_yaml()
    updated = 0
    async with session_factory() as session:
        for spec in specs:
            tcg_id = str(spec["tcg_card_id"])
            art = str(spec.get("image_art") or "cosmos")
            rarity_code = str(spec["rarity_class"])
            rid = await _rarity_id(session, rarity_code)
            small = resolve_public_image_url(manual_card_static_path(art, large=False), web_public_url)
            large = resolve_public_image_url(manual_card_static_path(art, large=True), web_public_url)

            card = await session.scalar(
                select(Card).where(Card.tcg_card_id == tcg_id)
            )
            if card is None:
                card = Card(
                    tcg_card_id=tcg_id,
                    name=str(spec["name"]),
                    set_code=_SET_CODE,
                    set_name=_SET_NAME,
                    collector_number=str(spec["collector_number"]),
                    tcg_rarity=str(spec.get("tcg_rarity") or "Promo"),
                    image_small_url=small,
                    image_large_url=large,
                    supertype=_SUPERTYPE,
                    hp=_HP,
                    attacks=list(_ANCIENT_MEW_ATTACKS),
                    dex_numbers=list(_DEX),
                    tcg_types=list(_TYPES),
                    rarity_class_id=rid,
                )
                session.add(card)
            else:
                card.name = str(spec["name"])
                card.tcg_rarity = str(spec.get("tcg_rarity") or "Promo")
                card.image_small_url = small
                card.image_large_url = large
                card.rarity_class_id = rid
            updated += 1
        await session.commit()
    _LOG.info("Upserted %s Ancient Mew card(s).", updated)
    return updated
