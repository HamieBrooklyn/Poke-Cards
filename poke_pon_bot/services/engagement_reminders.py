"""Scheduled engagement reminders (daily claim, card drop cooldown, Top.gg vote)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.pokedollars import UserPokedollars
from poke_pon_bot.models.user_web_preferences import UserWebPreferences
from poke_pon_bot.services.notification_delivery import (
    PREF_DAILY,
    PREF_DROP,
    PREF_VOTE,
    schedule_notification,
)
from poke_pon_bot.services.stripe_shop import user_has_stripe_drop_boost
from poke_pon_bot.services.wallet import _claimed_today_utc

_LOG = logging.getLogger(__name__)

VOTE_REMINDER_INTERVAL = timedelta(hours=12)
BATCH_SIZE = 150


@dataclass(frozen=True)
class PendingEngagementReminder:
    user_id: int
    kind: str
    title: str
    body: str
    href: str | None
    discord_pref: str
    discord_body: str


def _utc_today_key() -> str:
    return datetime.now(UTC).date().isoformat()


async def clear_drop_reminder_after_drop(
    session: AsyncSession,
    discord_user_id: int,
) -> None:
    """Allow a new drop-ready reminder after the user uses `/cd`."""
    row = await session.get(UserWebPreferences, int(discord_user_id))
    if row is not None:
        row.drop_reminder_sent_at = None


async def process_engagement_reminders(
    bot: Any,
    session_factory: async_sessionmaker,
    *,
    drop_cooldown_base_seconds: float,
    drop_cooldown_premium_seconds: float,
    drop_boost_test_user_ids: frozenset[int],
    topgg_vote_url: str,
    web_frontend_url: str | None,
) -> int:
    """Run one pass; returns count of reminders queued."""
    now = datetime.now(UTC)
    today_key = _utc_today_key()
    vote_cutoff = now - VOTE_REMINDER_INTERVAL
    base = (web_frontend_url or "").rstrip("/")
    settings_url = f"{base}/settings/" if base else None
    pending: list[PendingEngagementReminder] = []

    async with session_factory() as session:
        stmt = (
            select(UserPokedollars, UserWebPreferences)
            .outerjoin(
                UserWebPreferences,
                UserWebPreferences.discord_user_id == UserPokedollars.discord_user_id,
            )
            .where(
                or_(
                    UserWebPreferences.discord_user_id.is_(None),
                    UserWebPreferences.notify_daily.is_(True),
                    UserWebPreferences.notify_vote.is_(True),
                    UserWebPreferences.notify_drop.is_(True),
                )
            )
            .limit(BATCH_SIZE)
        )
        rows = (await session.execute(stmt)).all()

        for wallet, prefs in rows:
            uid = int(wallet.discord_user_id)
            if prefs is None:
                prefs = UserWebPreferences(discord_user_id=uid)
                session.add(prefs)

            notify_daily = bool(getattr(prefs, "notify_daily", False))
            notify_vote = bool(getattr(prefs, "notify_vote", True))
            notify_drop = bool(getattr(prefs, "notify_drop", False))

            if notify_daily:
                last_daily = wallet.last_daily_claim_at
                if not _claimed_today_utc(last_daily):
                    if prefs.daily_reminder_sent_key != today_key:
                        prefs.daily_reminder_sent_key = today_key
                        pending.append(
                            PendingEngagementReminder(
                                user_id=uid,
                                kind="daily_ready",
                                title="Daily reward ready",
                                body="Your daily ₽ claim is waiting — use `/daily` in Discord.",
                                href=settings_url,
                                discord_pref=PREF_DAILY,
                                discord_body=(
                                    "💰 **Daily reward ready!**\n"
                                    "Use **`/daily`** in Discord to claim your Pokedollars."
                                ),
                            )
                        )

            if notify_drop and wallet.last_drop_at is not None:
                last_drop = wallet.last_drop_at
                if last_drop.tzinfo is None:
                    last_drop = last_drop.replace(tzinfo=UTC)
                has_boost = uid in drop_boost_test_user_ids or await user_has_stripe_drop_boost(
                    session, discord_user_id=uid
                )
                cooldown = (
                    float(drop_cooldown_premium_seconds)
                    if has_boost
                    else float(drop_cooldown_base_seconds)
                )
                ready_at = last_drop + timedelta(seconds=cooldown)
                if now >= ready_at:
                    sent_at = prefs.drop_reminder_sent_at
                    if sent_at is None or sent_at < ready_at:
                        prefs.drop_reminder_sent_at = now
                        pending.append(
                            PendingEngagementReminder(
                                user_id=uid,
                                kind="drop_ready",
                                title="Card drop ready",
                                body="Your `/cd` cooldown has finished — grab your next cards!",
                                href=settings_url,
                                discord_pref=PREF_DROP,
                                discord_body=(
                                    "🃏 **Card drop ready!**\n"
                                    "Your **`/cd`** cooldown is over — open a new pack in Discord."
                                ),
                            )
                        )

            if notify_vote:
                last_sent = prefs.vote_reminder_sent_at
                if last_sent is None or last_sent < vote_cutoff:
                    prefs.vote_reminder_sent_at = now
                    vote_href = topgg_vote_url or None
                    pending.append(
                        PendingEngagementReminder(
                            user_id=uid,
                            kind="vote_reminder",
                            title="Vote on Top.gg",
                            body="Vote for PokePon on Top.gg for bonus ₽ and Crystals.",
                            href=vote_href,
                            discord_pref=PREF_VOTE,
                            discord_body=(
                                "🗳️ **Vote reminder**\n"
                                "Support PokePon on **Top.gg** for bonus rewards — use **`/vote`** "
                                f"or vote here: {topgg_vote_url}"
                            ),
                        )
                    )

        await session.commit()

    for item in pending:
        schedule_notification(
            bot,
            user_id=item.user_id,
            kind=item.kind,
            title=item.title,
            body=item.body,
            href=item.href,
            discord_pref=item.discord_pref,
            discord_body=item.discord_body,
        )

    if pending:
        _LOG.info("engagement reminders queued=%s", len(pending))
    return len(pending)
