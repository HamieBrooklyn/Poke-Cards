"""Grant Top.gg vote rewards without requiring ``/vote`` — on command use or webhook."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import discord
import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.config import Settings
from poke_pon_bot.services.topgg_vote import (
    TopggAuthError,
    TopggRateLimitError,
    fetch_active_discord_vote,
)
from poke_pon_bot.services.user_notifications import create_notification
from poke_pon_bot.services.web_preferences import get_or_create_web_preferences
from poke_pon_bot.services.wallet import (
    AlreadyClaimedVoteRewardError,
    WalletService,
    format_pokedollars,
)

_LOG = logging.getLogger(__name__)

_SKIP_COMMAND_NAMES = frozenset({"vote", "ppvote"})
_SKIP_COG_CLASS_NAMES = frozenset({"DevCog"})

# Per-user Top.gg API poll throttle (monotonic seconds).
_last_poll_at: dict[int, float] = {}


@dataclass(frozen=True)
class VoteRewardNotice:
    amount: int
    new_balance: int
    crystals_credited: int
    vote_url: str


def should_auto_claim_after_command(
    *,
    cog: object | None,
    command_name: str | None,
) -> bool:
    if command_name and command_name.lower() in _SKIP_COMMAND_NAMES:
        return False
    if cog is not None:
        name = getattr(type(cog), "__name__", "") or ""
        if name in _SKIP_COG_CLASS_NAMES:
            return False
    return True


def _poll_allowed(user_id: int, *, min_interval_seconds: float) -> bool:
    now = time.monotonic()
    last = _last_poll_at.get(user_id, 0.0)
    if now - last < min_interval_seconds:
        return False
    _last_poll_at[user_id] = now
    return True


async def claim_active_topgg_vote(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    discord_user_id: int,
) -> VoteRewardNotice | None:
    """
    If the user has an active Top.gg vote that has not been rewarded yet, credit them.

    Returns ``None`` when there is no active vote, the vote was already rewarded, or Top.gg
  could not be reached.
    """
    token = settings.topgg_api_token
    if not token:
        return None

    try:
        status = await fetch_active_discord_vote(token, discord_user_id)
    except TopggAuthError:
        _LOG.error("Top.gg vote auto-claim: API token rejected (401).")
        return None
    except TopggRateLimitError:
        _LOG.debug("Top.gg rate-limited vote check for user %s", discord_user_id)
        return None
    except httpx.HTTPError:
        _LOG.debug("Top.gg vote check failed for user %s", discord_user_id)
        return None

    if status is None:
        return None

    wallet = WalletService()
    try:
        async with session_factory() as session:
            try:
                result = await wallet.try_vote_claim(
                    session,
                    discord_user_id,
                    vote_created_at=status.created_at,
                    vote_expires_at=status.expires_at,
                )
                await session.commit()
            except AlreadyClaimedVoteRewardError:
                await session.rollback()
                return None
            except SQLAlchemyError:
                await session.rollback()
                raise
    except SQLAlchemyError:
        _LOG.exception("vote auto-claim DB error for user %s", discord_user_id)
        return None

    return VoteRewardNotice(
        amount=result.amount,
        new_balance=result.new_balance,
        crystals_credited=result.crystals_credited,
        vote_url=settings.topgg_vote_url,
    )


async def try_auto_claim_after_command(
    bot: discord.Client,
    user: discord.abc.User,
    *,
    command_name: str | None,
    cog: object | None,
) -> None:
    """Poll Top.gg (throttled) and DM the user when a new vote reward is credited."""
    settings = getattr(bot, "settings", None)
    if settings is None or not settings.topgg_api_token or not settings.topgg_auto_claim_enabled:
        return
    if user.bot:
        return
    if not should_auto_claim_after_command(cog=cog, command_name=command_name):
        return
    if not _poll_allowed(user.id, min_interval_seconds=settings.topgg_vote_poll_min_seconds):
        return

    notice = await claim_active_topgg_vote(
        bot.async_session_factory,
        settings,
        user.id,
    )
    if notice is None:
        return
    await notify_vote_reward_dm(bot, user, notice)


async def notify_vote_reward_dm(
    bot: discord.Client,
    user: discord.abc.User,
    notice: VoteRewardNotice,
) -> None:
    crystal_part = (
        f" · +**{notice.crystals_credited}** 💎" if notice.crystals_credited else ""
    )

    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(
            label="Vote on Top.gg",
            style=discord.ButtonStyle.link,
            url=notice.vote_url,
        )
    )

    text = (
        "Thanks for voting on **Top.gg**!\n\n"
        f"You received **{format_pokedollars(notice.amount)}**{crystal_part}.\n"
        f"**Balance:** {format_pokedollars(notice.new_balance)}"
    )
    inbox_body = (
        f"You received {format_pokedollars(notice.amount)}{crystal_part}. "
        f"Balance: {format_pokedollars(notice.new_balance)}."
    )

    send_dm = True
    factory = getattr(bot, "async_session_factory", None)
    settings = getattr(bot, "settings", None)
    web_notify = bool(getattr(settings, "web_notifications_enabled", False))
    if factory is not None and web_notify:
        try:
            async with factory() as session:
                await create_notification(
                    session,
                    discord_user_id=int(user.id),
                    kind="vote_reward",
                    title="Top.gg vote reward",
                    body=inbox_body,
                    href=notice.vote_url,
                )
                prefs = await get_or_create_web_preferences(session, int(user.id))
                send_dm = bool(getattr(prefs, "notify_vote", True))
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("vote reward inbox user=%s", user.id)
    elif factory is not None:
        try:
            async with factory() as session:
                prefs = await get_or_create_web_preferences(session, int(user.id))
                send_dm = bool(getattr(prefs, "notify_vote", True))
        except SQLAlchemyError:
            pass

    if not send_dm:
        return

    try:
        dm_user = user if isinstance(user, discord.User) else await bot.fetch_user(user.id)
    except (discord.NotFound, discord.HTTPException):
        _LOG.warning("Could not fetch user %s for vote reward DM", user.id)
        return

    try:
        await dm_user.send(text, view=view)
    except discord.Forbidden:
        _LOG.info("Vote reward DM blocked for user %s", user.id)
    except discord.HTTPException:
        _LOG.exception("Vote reward DM failed for user %s", user.id)
