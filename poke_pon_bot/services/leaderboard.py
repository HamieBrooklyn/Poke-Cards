"""Leaderboard queries — shared by Discord commands and the public web API."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Integer, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.auction import AUCTION_STATUS_ENDED_SOLD, CardAuction
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.rarity import RarityClass

LEADERBOARD_CATEGORIES = frozenset({"strongest", "tankiest", "rarest", "auction"})

LEADERBOARD_TITLES = {
    "strongest": "Strongest Cards",
    "tankiest": "Tankiest Cards",
    "rarest": "Rarest Cards",
    "auction": "Top Auction Sales",
}

DISCORD_PER_PAGE = 10
DISCORD_MAX_PAGES = 50
MAX_ENTRIES = DISCORD_PER_PAGE * DISCORD_MAX_PAGES
FETCH_LIMIT = 50_000


def max_attack_damage(attacks: Any) -> int:
    if not isinstance(attacks, list):
        return 0
    best = 0
    for atk in attacks:
        if not isinstance(atk, dict):
            continue
        raw = atk.get("damage")
        if raw is None:
            continue
        digits: list[str] = []
        for ch in str(raw):
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            best = max(best, int("".join(digits)))
    return best


async def leaderboard_strongest(
    session: AsyncSession,
    member_ids: set[int] | None,
) -> list[tuple[int, str, int]]:
    stmt = (
        select(UserCardInstance.discord_user_id, Card.name, Card.attacks)
        .join(Card, Card.id == UserCardInstance.card_id)
        .where(Card.attacks.isnot(None))
    )
    if member_ids is not None:
        stmt = stmt.where(UserCardInstance.discord_user_id.in_(member_ids))
    rows = (await session.execute(stmt.limit(FETCH_LIMIT))).all()

    best: dict[int, tuple[str, int]] = {}
    for uid, name, attacks in rows:
        dmg = max_attack_damage(attacks)
        if dmg <= 0:
            continue
        prev = best.get(uid)
        if prev is None or dmg > prev[1]:
            best[uid] = (name, dmg)

    ranked = sorted(best.items(), key=lambda x: x[1][1], reverse=True)[:MAX_ENTRIES]
    return [(uid, name, dmg) for uid, (name, dmg) in ranked]


async def leaderboard_tankiest(
    session: AsyncSession,
    member_ids: set[int] | None,
) -> list[tuple[int, str, int]]:
    hp_int = func.cast(func.coalesce(func.nullif(Card.hp, ""), "0"), Integer())
    stmt = (
        select(UserCardInstance.discord_user_id, Card.name, hp_int.label("hp_val"))
        .join(Card, Card.id == UserCardInstance.card_id)
        .where(Card.hp.isnot(None), Card.hp != "")
        .order_by(desc("hp_val"))
    )
    if member_ids is not None:
        stmt = stmt.where(UserCardInstance.discord_user_id.in_(member_ids))
    rows = (await session.execute(stmt.limit(FETCH_LIMIT))).all()

    best: dict[int, tuple[str, int]] = {}
    for uid, name, hp_val in rows:
        hp = int(hp_val) if hp_val else 0
        if hp <= 0:
            continue
        prev = best.get(uid)
        if prev is None or hp > prev[1]:
            best[uid] = (name, hp)

    ranked = sorted(best.items(), key=lambda x: x[1][1], reverse=True)[:MAX_ENTRIES]
    return [(uid, name, hp) for uid, (name, hp) in ranked]


async def leaderboard_rarest(
    session: AsyncSession,
    member_ids: set[int] | None,
) -> list[tuple[int, str, str, int]]:
    stmt = (
        select(
            UserCardInstance.discord_user_id,
            Card.name,
            RarityClass.display_name,
            RarityClass.sort_order,
        )
        .join(Card, Card.id == UserCardInstance.card_id)
        .join(RarityClass, RarityClass.id == Card.rarity_class_id)
        .order_by(desc(RarityClass.sort_order), desc(UserCardInstance.obtained_at))
    )
    if member_ids is not None:
        stmt = stmt.where(UserCardInstance.discord_user_id.in_(member_ids))
    rows = (await session.execute(stmt.limit(FETCH_LIMIT))).all()

    best: dict[int, tuple[str, str, int]] = {}
    for uid, name, rarity_name, sort_order in rows:
        order = int(sort_order) if sort_order else 0
        prev = best.get(uid)
        if prev is None or order > prev[2]:
            best[uid] = (name, rarity_name or "Unknown", order)

    ranked = sorted(best.items(), key=lambda x: x[1][2], reverse=True)[:MAX_ENTRIES]
    return [(uid, name, rn, so) for uid, (name, rn, so) in ranked]


async def leaderboard_auction(
    session: AsyncSession,
    member_ids: set[int] | None,
    guild_id: int | None,
) -> list[tuple[int, str, int]]:
    stmt = (
        select(
            CardAuction.seller_discord_id,
            Card.name,
            CardAuction.high_bid_pokedollars,
        )
        .join(UserCardInstance, UserCardInstance.id == CardAuction.instance_id)
        .join(Card, Card.id == UserCardInstance.card_id)
        .where(CardAuction.status == AUCTION_STATUS_ENDED_SOLD)
        .order_by(desc(CardAuction.high_bid_pokedollars))
    )
    if member_ids is not None:
        stmt = stmt.where(CardAuction.seller_discord_id.in_(member_ids))
    if guild_id is not None:
        stmt = stmt.where(CardAuction.guild_id == guild_id)

    rows = (await session.execute(stmt.limit(MAX_ENTRIES))).all()
    return [(uid, name, int(price)) for uid, name, price in rows]


async def fetch_leaderboard(
    session: AsyncSession,
    category: str,
    *,
    member_ids: set[int] | None = None,
    guild_id: int | None = None,
) -> list[tuple]:
    if category == "strongest":
        return await leaderboard_strongest(session, member_ids)
    if category == "tankiest":
        return await leaderboard_tankiest(session, member_ids)
    if category == "rarest":
        return await leaderboard_rarest(session, member_ids)
    if category == "auction":
        return await leaderboard_auction(session, member_ids, guild_id)
    raise ValueError(f"unknown leaderboard category: {category}")


def viewer_rank(entries: list[tuple], viewer_id: int, category: str) -> int | None:
    """1-based rank of viewer in the full list, or None if not listed."""
    uid_index = 0
    for i, entry in enumerate(entries):
        if entry[uid_index] == viewer_id:
            return i + 1
    return None


def card_preview(
    card: Card,
    inst: UserCardInstance,
    *,
    rarity_display: str | None = None,
) -> dict[str, Any]:
    """JSON-safe card block for the website leaderboard modal."""
    return {
        "public_id": inst.public_id,
        "name": card.name,
        "set_name": card.set_name,
        "set_code": card.set_code,
        "collector_number": card.collector_number,
        "image_small_url": card.image_small_url,
        "image_large_url": card.image_large_url,
        "hp": card.hp,
        "tcg_rarity": card.tcg_rarity,
        "rarity_display": rarity_display,
    }


async def leaderboard_strongest_web(
    session: AsyncSession,
    member_ids: set[int] | None,
) -> list[tuple[int, str, int, dict[str, Any]]]:
    stmt = (
        select(UserCardInstance, Card, RarityClass.display_name)
        .join(Card, Card.id == UserCardInstance.card_id)
        .join(RarityClass, RarityClass.id == Card.rarity_class_id)
        .where(Card.attacks.isnot(None))
    )
    if member_ids is not None:
        stmt = stmt.where(UserCardInstance.discord_user_id.in_(member_ids))
    rows = (await session.execute(stmt.limit(FETCH_LIMIT))).all()

    best: dict[int, tuple[str, int, dict[str, Any]]] = {}
    for inst, card, rarity_name in rows:
        uid = int(inst.discord_user_id)
        dmg = max_attack_damage(card.attacks)
        if dmg <= 0:
            continue
        prev = best.get(uid)
        if prev is None or dmg > prev[1]:
            best[uid] = (
                card.name,
                dmg,
                card_preview(card, inst, rarity_display=rarity_name),
            )

    ranked = sorted(best.items(), key=lambda x: x[1][1], reverse=True)[:MAX_ENTRIES]
    return [(uid, name, dmg, preview) for uid, (name, dmg, preview) in ranked]


async def leaderboard_tankiest_web(
    session: AsyncSession,
    member_ids: set[int] | None,
) -> list[tuple[int, str, int, dict[str, Any]]]:
    hp_int = func.cast(func.coalesce(func.nullif(Card.hp, ""), "0"), Integer())
    stmt = (
        select(UserCardInstance, Card, hp_int.label("hp_val"), RarityClass.display_name)
        .join(Card, Card.id == UserCardInstance.card_id)
        .join(RarityClass, RarityClass.id == Card.rarity_class_id)
        .where(Card.hp.isnot(None), Card.hp != "")
        .order_by(desc("hp_val"))
    )
    if member_ids is not None:
        stmt = stmt.where(UserCardInstance.discord_user_id.in_(member_ids))
    rows = (await session.execute(stmt.limit(FETCH_LIMIT))).all()

    best: dict[int, tuple[str, int, dict[str, Any]]] = {}
    for inst, card, hp_val, rarity_name in rows:
        uid = int(inst.discord_user_id)
        hp = int(hp_val) if hp_val else 0
        if hp <= 0:
            continue
        prev = best.get(uid)
        if prev is None or hp > prev[1]:
            best[uid] = (
                card.name,
                hp,
                card_preview(card, inst, rarity_display=rarity_name),
            )

    ranked = sorted(best.items(), key=lambda x: x[1][1], reverse=True)[:MAX_ENTRIES]
    return [(uid, name, hp, preview) for uid, (name, hp, preview) in ranked]


async def leaderboard_rarest_web(
    session: AsyncSession,
    member_ids: set[int] | None,
) -> list[tuple[int, str, str, int, dict[str, Any]]]:
    stmt = (
        select(
            UserCardInstance,
            Card,
            RarityClass.display_name,
            RarityClass.sort_order,
        )
        .join(Card, Card.id == UserCardInstance.card_id)
        .join(RarityClass, RarityClass.id == Card.rarity_class_id)
        .order_by(desc(RarityClass.sort_order), desc(UserCardInstance.obtained_at))
    )
    if member_ids is not None:
        stmt = stmt.where(UserCardInstance.discord_user_id.in_(member_ids))
    rows = (await session.execute(stmt.limit(FETCH_LIMIT))).all()

    best: dict[int, tuple[str, str, int, dict[str, Any]]] = {}
    for inst, card, rarity_name, sort_order in rows:
        uid = int(inst.discord_user_id)
        order = int(sort_order) if sort_order else 0
        prev = best.get(uid)
        if prev is None or order > prev[2]:
            best[uid] = (
                card.name,
                rarity_name or "Unknown",
                order,
                card_preview(card, inst, rarity_display=rarity_name),
            )

    ranked = sorted(best.items(), key=lambda x: x[1][2], reverse=True)[:MAX_ENTRIES]
    return [(uid, name, rn, so, preview) for uid, (name, rn, so, preview) in ranked]


async def leaderboard_auction_web(
    session: AsyncSession,
    member_ids: set[int] | None,
    guild_id: int | None,
) -> list[tuple[int, str, int, dict[str, Any]]]:
    stmt = (
        select(
            CardAuction.seller_discord_id,
            CardAuction.high_bid_pokedollars,
            UserCardInstance,
            Card,
            RarityClass.display_name,
        )
        .join(UserCardInstance, UserCardInstance.id == CardAuction.instance_id)
        .join(Card, Card.id == UserCardInstance.card_id)
        .join(RarityClass, RarityClass.id == Card.rarity_class_id)
        .where(CardAuction.status == AUCTION_STATUS_ENDED_SOLD)
        .order_by(desc(CardAuction.high_bid_pokedollars))
    )
    if member_ids is not None:
        stmt = stmt.where(CardAuction.seller_discord_id.in_(member_ids))
    if guild_id is not None:
        stmt = stmt.where(CardAuction.guild_id == guild_id)

    rows = (await session.execute(stmt.limit(MAX_ENTRIES))).all()
    out: list[tuple[int, str, int, dict[str, Any]]] = []
    for seller_id, price, inst, card, rarity_name in rows:
        uid = int(seller_id)
        preview = card_preview(card, inst, rarity_display=rarity_name)
        out.append((uid, card.name, int(price), preview))
    return out


async def fetch_leaderboard_web(
    session: AsyncSession,
    category: str,
    *,
    member_ids: set[int] | None = None,
    guild_id: int | None = None,
) -> list[tuple]:
    if category == "strongest":
        return await leaderboard_strongest_web(session, member_ids)
    if category == "tankiest":
        return await leaderboard_tankiest_web(session, member_ids)
    if category == "rarest":
        return await leaderboard_rarest_web(session, member_ids)
    if category == "auction":
        return await leaderboard_auction_web(session, member_ids, guild_id)
    raise ValueError(f"unknown leaderboard category: {category}")
