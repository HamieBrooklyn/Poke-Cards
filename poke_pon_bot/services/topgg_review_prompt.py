"""One-time Top.gg review reminder DM after meaningful bot usage."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.config import Settings
from poke_pon_bot.models.user_engagement import UserEngagement

_LOG = logging.getLogger(__name__)

# Cogs whose commands should not count toward "getting into" the bot.
_SKIP_COG_QUALIFIED_NAMES = frozenset({"DevCog"})


def review_page_url(settings: Settings, *, application_id: int | None) -> str:
    if settings.topgg_review_url:
        return settings.topgg_review_url
    if application_id is not None:
        return f"https://top.gg/bot/{int(application_id)}/reviews"
    return "https://top.gg/"


def should_track_command(*, cog: object | None) -> bool:
    if cog is None:
        return True
    name = getattr(type(cog), "__name__", "") or ""
    if name in _SKIP_COG_QUALIFIED_NAMES:
        return False
    return True


async def _get_or_create(session: AsyncSession, discord_user_id: int) -> UserEngagement:
    row = await session.get(UserEngagement, discord_user_id)
    if row is None:
        row = UserEngagement(discord_user_id=discord_user_id, successful_commands=0)
        session.add(row)
        await session.flush()
    return row


async def record_successful_command(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    discord_user_id: int,
    min_commands: int,
) -> bool:
    """
    Increment the user's successful-command count.

    Returns ``True`` when they are at or above ``min_commands``, have not been
    reminded yet, and have not acknowledged a review (eligible for a DM).
    """
    async with session_factory() as session:
        row = await _get_or_create(session, discord_user_id)
        if row.topgg_review_acknowledged_at is not None:
            await session.commit()
            return False
        if row.topgg_review_reminder_sent_at is not None:
            await session.commit()
            return False
        row.successful_commands = int(row.successful_commands or 0) + 1
        ready = row.successful_commands >= min_commands
        await session.commit()
        return ready


async def mark_review_reminder_sent(
    session_factory: async_sessionmaker[AsyncSession],
    discord_user_id: int,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        row = await _get_or_create(session, discord_user_id)
        row.topgg_review_reminder_sent_at = now
        await session.commit()


async def mark_review_acknowledged(
    session_factory: async_sessionmaker[AsyncSession],
    discord_user_id: int,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        row = await _get_or_create(session, discord_user_id)
        row.topgg_review_acknowledged_at = now
        await session.commit()


class TopggReviewAckView(discord.ui.View):
    """DM buttons: open Top.gg reviews + stop reminders after self-ack."""

    def __init__(
        self,
        *,
        owner_id: int,
        review_url: str,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Leave a review on Top.gg",
                style=discord.ButtonStyle.link,
                url=review_url,
            )
        )
        ack = discord.ui.Button(
            label="I've left a review",
            style=discord.ButtonStyle.secondary,
            custom_id=f"pokepon:topgg_review_ack:{owner_id}",
        )
        self.add_item(ack)


def build_review_reminder_embed(user: discord.abc.User) -> discord.Embed:
    return discord.Embed(
        title="Enjoying PokePon?",
        description=(
            f"{user.mention} thanks for playing!\n\n"
            "If you have a moment, a short **review on Top.gg** helps more players "
            "discover the bot.\n\n"
            "Already reviewed? Tap **I've left a review** so we don't remind you again."
        ),
        colour=discord.Colour.blurple(),
    )


async def send_review_reminder_dm(
    bot: discord.Client,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: discord.User | discord.Member,
) -> bool:
    """Try to DM ``user``; return ``True`` if the message was sent."""
    url = review_page_url(settings, application_id=bot.application_id)
    embed = build_review_reminder_embed(user)
    view = TopggReviewAckView(owner_id=user.id, review_url=url)
    try:
        await user.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions(users=True))
    except discord.Forbidden:
        _LOG.info("Top.gg review reminder skipped — DMs closed for user %s", user.id)
        return False
    except discord.HTTPException:
        _LOG.exception("Top.gg review reminder DM failed for user %s", user.id)
        return False
    return True


async def _already_handled_review_prompt(
    session_factory: async_sessionmaker[AsyncSession],
    discord_user_id: int,
) -> bool:
    async with session_factory() as session:
        row = await session.get(UserEngagement, discord_user_id)
        if row is None:
            return False
        return (
            row.topgg_review_acknowledged_at is not None
            or row.topgg_review_reminder_sent_at is not None
        )


async def try_prompt_after_command(
    bot: discord.Client,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user: discord.User | discord.Member,
) -> None:
    if not settings.topgg_review_prompt_enabled:
        return
    if user.bot:
        return
    ready = await record_successful_command(
        session_factory,
        discord_user_id=user.id,
        min_commands=settings.topgg_review_prompt_min_commands,
    )
    if not ready:
        return
    if await _already_handled_review_prompt(session_factory, user.id):
        return

    sent = await send_review_reminder_dm(
        bot,
        session_factory=session_factory,
        settings=settings,
        user=user,
    )
    if sent:
        await mark_review_reminder_sent(session_factory, user.id)
