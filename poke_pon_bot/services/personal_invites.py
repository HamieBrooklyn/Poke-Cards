"""Per-user Discord invite codes.

Sharing one server invite (``discord.gg/abc``) makes referral attribution
unreliable because:

* Vanity URLs and the server's default invite do not have an ``Invite.inviter``
  recorded by Discord.
* When multiple users share the same link, the bot can only see one creator —
  every joining friend gets attributed to that single user.

To fix this we mint a personal invite code for each user (``unique=True``) and
store the ``code → user`` mapping ourselves. The referral listener then maps
the used code back to the inviter directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.user_invite_code import UserInviteCode

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersonalInvite:
    code: str
    url: str
    guild_id: int
    channel_id: int


def _invite_url(code: str) -> str:
    return f"https://discord.gg/{code}"


def _sync_tracker(bot: discord.Client, *, guild_id: int, code: str, uses: int = 0) -> None:
    try:
        from poke_pon_bot.referral_listener import sync_invite_tracker_uses

        sync_invite_tracker_uses(bot, guild_id=guild_id, invite_code=code, uses=uses)
    except Exception:
        _LOG.debug("Could not sync invite tracker for code %s", code, exc_info=True)


async def find_inviter_by_code(
    session: AsyncSession, *, guild_id: int, invite_code: str
) -> int | None:
    """Return the Discord user id that owns the given invite code, if any."""
    row = await session.execute(
        select(UserInviteCode.discord_user_id).where(
            UserInviteCode.guild_id == guild_id,
            UserInviteCode.invite_code == invite_code,
        )
    )
    value = row.scalar_one_or_none()
    return int(value) if value is not None else None


async def get_personal_invite_row(
    session: AsyncSession, *, discord_user_id: int, guild_id: int
) -> UserInviteCode | None:
    row = await session.execute(
        select(UserInviteCode).where(
            UserInviteCode.discord_user_id == discord_user_id,
            UserInviteCode.guild_id == guild_id,
        )
    )
    return row.scalar_one_or_none()


def _pick_invite_channel(
    guild: discord.Guild, preferred_channel_id: int | None
) -> discord.TextChannel | None:
    """Pick a text channel where the bot can create invites."""
    me = guild.me
    if me is None:
        return None

    if preferred_channel_id is not None:
        ch = guild.get_channel(int(preferred_channel_id))
        if (
            isinstance(ch, discord.TextChannel)
            and ch.permissions_for(me).create_instant_invite
        ):
            return ch

    for ch in guild.text_channels:
        if ch.permissions_for(me).create_instant_invite:
            return ch
    return None


async def get_or_create_personal_invite(
    bot: discord.Client,
    session: AsyncSession,
    *,
    discord_user_id: int,
    guild_id: int,
    preferred_channel_id: int | None = None,
) -> PersonalInvite | None:
    """Return (or mint) a personal invite for ``discord_user_id`` in ``guild_id``.

    Returns ``None`` when the bot can't access the guild or lacks the
    ``Create Instant Invite`` permission on any text channel.
    """
    existing = await get_personal_invite_row(
        session, discord_user_id=discord_user_id, guild_id=guild_id
    )

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        try:
            guild = await bot.fetch_guild(int(guild_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _LOG.warning(
                "Cannot reach guild %s to mint a personal invite for %s",
                guild_id,
                discord_user_id,
            )
            if existing is not None:
                return PersonalInvite(
                    code=existing.invite_code,
                    url=_invite_url(existing.invite_code),
                    guild_id=int(existing.guild_id),
                    channel_id=int(existing.channel_id),
                )
            return None

    if existing is not None:
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            invites = []
        codes = {inv.code for inv in invites if inv.code}
        if existing.invite_code in codes:
            uses = next(
                (int(inv.uses or 0) for inv in invites if inv.code == existing.invite_code),
                0,
            )
            _sync_tracker(
                bot, guild_id=int(guild_id), code=existing.invite_code, uses=uses
            )
            return PersonalInvite(
                code=existing.invite_code,
                url=_invite_url(existing.invite_code),
                guild_id=int(existing.guild_id),
                channel_id=int(existing.channel_id),
            )
        _LOG.info(
            "Personal invite code %s for user %s no longer exists in guild %s — minting a new one.",
            existing.invite_code,
            discord_user_id,
            guild_id,
        )

    channel = _pick_invite_channel(guild, preferred_channel_id)
    if channel is None:
        _LOG.warning(
            "No channel with Create Instant Invite in guild %s — cannot mint invite for %s",
            guild_id,
            discord_user_id,
        )
        if existing is not None:
            return PersonalInvite(
                code=existing.invite_code,
                url=_invite_url(existing.invite_code),
                guild_id=int(existing.guild_id),
                channel_id=int(existing.channel_id),
            )
        return None

    try:
        invite = await channel.create_invite(
            max_age=0,
            max_uses=0,
            unique=True,
            reason=f"Personal referral invite for user {discord_user_id}",
        )
    except discord.Forbidden:
        _LOG.warning(
            "Forbidden when creating invite in channel %s of guild %s",
            channel.id,
            guild_id,
        )
        return None
    except discord.HTTPException:
        _LOG.exception(
            "Failed to create invite in channel %s of guild %s",
            channel.id,
            guild_id,
        )
        return None

    try:
        if existing is not None:
            existing.channel_id = int(channel.id)
            existing.invite_code = invite.code
        else:
            session.add(
                UserInviteCode(
                    discord_user_id=int(discord_user_id),
                    guild_id=int(guild_id),
                    channel_id=int(channel.id),
                    invite_code=invite.code,
                )
            )
        await session.flush()
    except SQLAlchemyError:
        _LOG.exception(
            "Failed to persist personal invite %s for user %s",
            invite.code,
            discord_user_id,
        )
        return None

    _sync_tracker(
        bot, guild_id=int(guild_id), code=invite.code, uses=int(invite.uses or 0)
    )
    return PersonalInvite(
        code=invite.code,
        url=_invite_url(invite.code),
        guild_id=int(guild_id),
        channel_id=int(channel.id),
    )
