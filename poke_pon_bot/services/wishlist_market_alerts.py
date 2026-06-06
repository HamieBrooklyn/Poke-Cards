"""Discord DMs when a wishlisted card appears on auctions or trades."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.models.auction import (
    AUCTION_BID_CURRENCY_CRYSTALS,
    AUCTION_BID_CURRENCY_POKEDOLLARS,
)
from poke_pon_bot.models.user_web_preferences import UserWebPreferences
from poke_pon_bot.services.auction_runtime import auction_amount_display, normalize_auction_bid_currency
from poke_pon_bot.services.notification_delivery import (
    PREF_WISHLIST,
    schedule_notification,
)
from poke_pon_bot.services.web_preferences import get_or_create_web_preferences
from poke_pon_bot.services.wishlist import wishlist_user_ids_for_cards

_LOG = logging.getLogger(__name__)


def schedule_wishlist_auction_alert(
    bot: Any,
    *,
    seller_id: int,
    auction_id: int,
    catalog_card_id: int,
    card_name: str,
    price: int,
    currency: str,
) -> None:
    factory = getattr(bot, "async_session_factory", None)
    if factory is None:
        return
    settings = getattr(bot, "settings", None)
    base = (getattr(settings, "web_frontend_url", None) or "").rstrip("/")
    auctions_url = f"{base}/auctions/" if base else ""
    asyncio.create_task(
        _notify_wishlisters_for_auction(
            bot,
            session_factory=factory,
            seller_id=int(seller_id),
            auction_id=int(auction_id),
            catalog_card_id=int(catalog_card_id),
            card_name=str(card_name),
            price=int(price),
            currency=normalize_auction_bid_currency(currency) or AUCTION_BID_CURRENCY_POKEDOLLARS,
            auctions_url=auctions_url,
        ),
        name=f"wishlist-auction-{auction_id}",
    )


def schedule_wishlist_trade_alerts(
    bot: Any,
    *,
    offerer_id: int,
    cards: list[tuple[int, str]],
    exclude_user_ids: frozenset[int] | None = None,
    trade_id: int | None = None,
) -> None:
    if not cards:
        return
    factory = getattr(bot, "async_session_factory", None)
    if factory is None:
        return
    settings = getattr(bot, "settings", None)
    base = (getattr(settings, "web_frontend_url", None) or "").rstrip("/")
    trades_url = f"{base}/trades/" if base else ""
    asyncio.create_task(
        _notify_wishlisters_for_trade(
            bot,
            session_factory=factory,
            offerer_id=int(offerer_id),
            trade_id=int(trade_id) if trade_id is not None else None,
            cards=[(int(cid), str(name)) for cid, name in cards],
            exclude_user_ids=exclude_user_ids or frozenset(),
            trades_url=trades_url,
        ),
        name=f"wishlist-trade-{trade_id or 'discord'}",
    )


def _auction_price_passes_cap(prefs: UserWebPreferences, price: int, currency: str) -> bool:
    cur = normalize_auction_bid_currency(currency) or AUCTION_BID_CURRENCY_POKEDOLLARS
    if cur == AUCTION_BID_CURRENCY_CRYSTALS:
        cap = prefs.wishlist_alert_max_crystals
    else:
        cap = prefs.wishlist_alert_max_pokedollars
    if cap is None:
        return True
    return int(price) <= int(cap)


async def _load_prefs_map(
    session_factory,
    user_ids: list[int],
) -> dict[int, UserWebPreferences]:
    if not user_ids:
        return {}
    try:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(UserWebPreferences).where(
                        UserWebPreferences.discord_user_id.in_(user_ids)
                    )
                )
            ).scalars()
            out: dict[int, UserWebPreferences] = {}
            for row in rows:
                out[int(row.discord_user_id)] = row
            for uid in user_ids:
                if uid not in out:
                    out[uid] = await get_or_create_web_preferences(session, uid)
            await session.commit()
            return out
    except SQLAlchemyError:
        _LOG.exception("wishlist market prefs lookup")
        return {}


async def _notify_wishlisters_for_auction(
    bot: Any,
    *,
    session_factory,
    seller_id: int,
    auction_id: int,
    catalog_card_id: int,
    card_name: str,
    price: int,
    currency: str,
    auctions_url: str,
) -> None:
    try:
        async with session_factory() as session:
            by_card = await wishlist_user_ids_for_cards(
                session, [catalog_card_id], exclude_user_id=seller_id
            )
    except SQLAlchemyError:
        _LOG.exception("wishlist auction lookup auction=%s", auction_id)
        return

    user_ids = by_card.get(catalog_card_id, [])
    if not user_ids:
        return

    prefs_map = await _load_prefs_map(session_factory, user_ids)
    price_label = auction_amount_display(price, currency)
    for uid in user_ids:
        prefs = prefs_map.get(uid)
        if prefs is None or not prefs.notify_wishlist_market:
            continue
        if not _auction_price_passes_cap(prefs, price, currency):
            continue
        if auctions_url:
            body = (
                f"⭐ **Wishlist alert** — **{card_name}** was listed on auction "
                f"(min bid **{price_label}**).\n"
                f"Open **{auctions_url}?id={auction_id}** or use **`/auction bid`** with listing **`{auction_id}`**."
            )
            inbox_href = f"{auctions_url}?id={auction_id}"
        else:
            body = (
                f"⭐ **Wishlist alert** — **{card_name}** was listed on auction "
                f"(min bid **{price_label}**, listing **`{auction_id}`**).\n"
                "Use **`/auction search`** and **`/auction bid`** to bid."
            )
            inbox_href = None
        schedule_notification(
            bot,
            user_id=uid,
            kind="wishlist_hit",
            title="Wishlist: auction listing",
            body=f"{card_name} listed (min {price_label})",
            href=inbox_href,
            discord_pref=PREF_WISHLIST,
            discord_body=body,
        )


async def _notify_wishlisters_for_trade(
    bot: Any,
    *,
    session_factory,
    offerer_id: int,
    trade_id: int | None,
    cards: list[tuple[int, str]],
    exclude_user_ids: frozenset[int],
    trades_url: str,
) -> None:
    card_ids = [cid for cid, _ in cards]
    try:
        async with session_factory() as session:
            by_card = await wishlist_user_ids_for_cards(session, card_ids, exclude_user_id=None)
    except SQLAlchemyError:
        _LOG.exception("wishlist trade lookup trade=%s", trade_id)
        return

    names_by_id = {cid: name for cid, name in cards}
    per_user: dict[int, list[str]] = {}
    for cid, wishers in by_card.items():
        nm = names_by_id.get(cid)
        if not nm:
            continue
        for uid in wishers:
            if uid in exclude_user_ids or uid == offerer_id:
                continue
            per_user.setdefault(uid, []).append(nm)

    if not per_user:
        return

    prefs_map = await _load_prefs_map(session_factory, list(per_user.keys()))
    web_link = (
        f"{trades_url}?id={trade_id}"
        if trades_url and trade_id is not None
        else None
    )
    for uid, names in per_user.items():
        prefs = prefs_map.get(uid)
        if prefs is None or not prefs.notify_wishlist_market:
            continue
        unique = list(dict.fromkeys(names))
        if len(unique) == 1:
            card_line = f"**{unique[0]}**"
        else:
            card_line = ", ".join(f"**{n}**" for n in unique)
        verb = "is" if len(unique) == 1 else "are"
        if web_link:
            body = (
                f"⭐ **Wishlist alert** — {card_line} {verb} on offer in a trade.\n"
                f"Open **{web_link}** to view the session."
            )
            inbox_href = web_link
        else:
            body = (
                f"⭐ **Wishlist alert** — {card_line} {verb} on offer in a Discord trade.\n"
                "Check your servers for an open **`/trade`** offer."
            )
            inbox_href = None
        schedule_notification(
            bot,
            user_id=uid,
            kind="wishlist_hit",
            title="Wishlist: trade offer",
            body=f"{', '.join(unique)} on offer in a trade",
            href=inbox_href,
            discord_pref=PREF_WISHLIST,
            discord_body=body,
        )
