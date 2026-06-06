"""Deliver Discord DMs and website inbox rows (respecting notification prefs)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.user_notifications import create_notification
from poke_pon_bot.services.web_preferences import get_or_create_web_preferences

_LOG = logging.getLogger(__name__)

PREF_TRADES = "notify_trades"
PREF_AUCTIONS = "notify_auctions"
PREF_REFERRALS = "notify_referrals"
PREF_MISSIONS = "notify_missions"
PREF_WISHLIST = "notify_wishlist_market"
PREF_DAILY = "notify_daily"
PREF_VOTE = "notify_vote"
PREF_DROP = "notify_drop"


async def _send_discord_dm(bot: Any, user_id: int, text: str) -> None:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(text)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass
    except Exception:
        _LOG.debug("discord DM failed user=%s", user_id)


async def deliver_notification(
    bot: Any,
    session_factory,
    *,
    user_id: int,
    kind: str,
    title: str,
    body: str,
    href: str | None = None,
    discord_pref: str | None = None,
    discord_body: str | None = None,
) -> None:
    """Write an inbox row; optionally send a Discord DM when the pref allows."""
    try:
        async with session_factory() as session:
            await create_notification(
                session,
                discord_user_id=int(user_id),
                kind=kind,
                title=title,
                body=body,
                href=href,
            )
            send_discord = False
            if discord_pref is not None:
                prefs = await get_or_create_web_preferences(session, int(user_id))
                send_discord = bool(getattr(prefs, discord_pref, True))
            await session.commit()
    except SQLAlchemyError:
        _LOG.exception("inbox notification user=%s kind=%s", user_id, kind)
        return

    if discord_pref is not None and send_discord and discord_body:
        await _send_discord_dm(bot, int(user_id), discord_body)


def schedule_notification(
    bot: Any,
    *,
    user_id: int,
    kind: str,
    title: str,
    body: str,
    href: str | None = None,
    discord_pref: str | None = None,
    discord_body: str | None = None,
) -> None:
    settings = getattr(bot, "settings", None)
    if settings is None or not getattr(settings, "web_notifications_enabled", False):
        return
    factory = getattr(bot, "async_session_factory", None)
    if factory is None:
        return
    asyncio.create_task(
        deliver_notification(
            bot,
            factory,
            user_id=int(user_id),
            kind=kind,
            title=title,
            body=body,
            href=href,
            discord_pref=discord_pref,
            discord_body=discord_body,
        ),
        name=f"notify-{kind}-{user_id}",
    )


def schedule_outbid_alert(
    bot: Any,
    *,
    outbid_user_id: int,
    auction_id: int,
    card_name: str,
    new_amount_label: str,
    auctions_url: str,
) -> None:
    if int(outbid_user_id) <= 0:
        return
    href = f"{auctions_url}?id={auction_id}" if auctions_url else None
    title = "Outbid on auction"
    body = f"Someone bid {new_amount_label} on {card_name} (listing #{auction_id})."
    if href:
        discord = (
            f"⚖️ **Outbid!** **{card_name}** — new high bid **{new_amount_label}**.\n"
            f"Open **{href}** or use **`/auction bid {auction_id}`**."
        )
    else:
        discord = (
            f"⚖️ **Outbid!** **{card_name}** — new high bid **{new_amount_label}** "
            f"(listing **`{auction_id}`**).\n"
            "Use **`/auction search`** and **`/auction bid`** to respond."
        )
    schedule_notification(
        bot,
        user_id=int(outbid_user_id),
        kind="outbid",
        title=title,
        body=body,
        href=href,
        discord_pref=PREF_AUCTIONS,
        discord_body=discord,
    )
