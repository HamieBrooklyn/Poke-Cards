"""Track guild invites and attribute new members to referrers (no user-facing commands)."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from poke_pon_bot.services.personal_invites import find_inviter_by_code
from poke_pon_bot.services.referrals import (
    mark_first_guild_join,
    register_referral_join,
    seed_guild_members_seen,
)

_LOG = logging.getLogger(__name__)


class InviteTracker:
    """Per-guild invite use counts — diff on ``member_join`` to find the inviter."""

    def __init__(self) -> None:
        self._uses: dict[int, dict[str, int]] = {}

    def snapshot(self, guild_id: int) -> dict[str, int]:
        return dict(self._uses.get(guild_id, {}))

    def set_guild(self, guild_id: int, invites: dict[str, int]) -> None:
        self._uses[guild_id] = invites

    def update_code(self, guild_id: int, code: str, uses: int) -> None:
        self._uses.setdefault(guild_id, {})[code] = uses

    def remove_code(self, guild_id: int, code: str) -> None:
        bucket = self._uses.get(guild_id)
        if bucket is not None:
            bucket.pop(code, None)


async def _fetch_guild_invites(
    guild: discord.Guild,
) -> list[discord.Invite]:
    try:
        return await guild.invites()
    except discord.Forbidden:
        _LOG.warning(
            "Cannot read invites in guild %s (%s) — grant **Manage Server** so referral tracking works.",
            guild.id,
            guild.name,
        )
        return []
    except discord.HTTPException:
        _LOG.exception("Failed to fetch invites for guild %s", guild.id)
        return []


def _invite_uses_map(invites: list[discord.Invite]) -> dict[str, int]:
    out: dict[str, int] = {}
    for inv in invites:
        if inv.code:
            out[inv.code] = int(inv.uses or 0)
    return out


async def _cache_guild_invites(bot: commands.Bot, guild: discord.Guild) -> None:
    tracker: InviteTracker = bot.invite_tracker  # type: ignore[attr-defined]
    tracker.set_guild(guild.id, _invite_uses_map(await _fetch_guild_invites(guild)))


async def _seed_guild(bot: commands.Bot, guild: discord.Guild) -> None:
    try:
        async with bot.async_session_factory() as session:
            added = await seed_guild_members_seen(session, guild)
            await session.commit()
        if added:
            _LOG.info(
                "Referral anti-abuse: marked %s existing members as seen in guild %s",
                added,
                guild.id,
            )
    except Exception:
        _LOG.exception("Failed to seed guild_member_seen for guild %s", guild.id)


def setup_referral_listener(bot: commands.Bot) -> None:
    if getattr(bot, "_pokepon_referral_listener", False):
        return

    bot.invite_tracker = InviteTracker()  # type: ignore[attr-defined]

    # Use add_listener — @bot.event would be overwritten by tutorial_listener's handlers.
    async def _referral_on_ready() -> None:
        for guild in bot.guilds:
            await _cache_guild_invites(bot, guild)
            await _seed_guild(bot, guild)

    async def _referral_on_guild_join(guild: discord.Guild) -> None:
        await _cache_guild_invites(bot, guild)
        await _seed_guild(bot, guild)

    async def _referral_on_invite_create(invite: discord.Invite) -> None:
        if invite.guild is None or not invite.code:
            return
        tracker: InviteTracker = bot.invite_tracker  # type: ignore[attr-defined]
        tracker.update_code(invite.guild.id, invite.code, int(invite.uses or 0))

    async def _referral_on_invite_delete(invite: discord.Invite) -> None:
        if invite.guild is None or not invite.code:
            return
        tracker: InviteTracker = bot.invite_tracker  # type: ignore[attr-defined]
        tracker.remove_code(invite.guild.id, invite.code)

    async def _referral_on_member_join(member: discord.Member) -> None:
        if member.bot or member.guild is None:
            return

        guild = member.guild
        tracker: InviteTracker = bot.invite_tracker  # type: ignore[attr-defined]
        before = tracker.snapshot(guild.id)
        invites = await _fetch_guild_invites(guild)
        if not invites:
            _LOG.warning(
                "Referral join for invitee=%s guild=%s but guild.invites() returned "
                "nothing — grant the bot **Manage Server** so invite attribution works.",
                member.id,
                guild.id,
            )
        after = _invite_uses_map(invites)
        tracker.set_guild(guild.id, after)

        first_join = False
        created = False
        try:
            async with bot.async_session_factory() as session:
                first_join = await mark_first_guild_join(
                    session,
                    guild_id=guild.id,
                    discord_user_id=member.id,
                )
                await session.commit()
        except Exception:
            _LOG.exception(
                "guild_member_seen failed invitee=%s guild=%s",
                member.id,
                guild.id,
            )
            return

        used_code: str | None = None
        fallback_inviter_id: int | None = None
        for inv in invites:
            if not inv.code:
                continue
            if after.get(inv.code, 0) > before.get(inv.code, 0):
                used_code = inv.code
                if inv.inviter is not None:
                    fallback_inviter_id = inv.inviter.id
                break

        inviter_id: int | None = None
        if used_code is not None:
            try:
                async with bot.async_session_factory() as session:
                    inviter_id = await find_inviter_by_code(
                        session, guild_id=guild.id, invite_code=used_code
                    )
            except Exception:
                _LOG.exception(
                    "personal invite lookup failed code=%s guild=%s", used_code, guild.id
                )

        if inviter_id is None:
            inviter_id = fallback_inviter_id

        if inviter_id is None or inviter_id == member.id:
            _LOG.info(
                "Referral skipped (%s): invitee=%s guild=%s code=%s first_join=%s",
                "self_invite" if inviter_id == member.id else "no_inviter",
                member.id,
                guild.id,
                used_code,
                first_join,
            )
            return

        inviter_created_at = None
        try:
            inviter_user = bot.get_user(inviter_id) or await bot.fetch_user(inviter_id)
        except (discord.NotFound, discord.HTTPException):
            inviter_user = None
        if inviter_user is not None:
            inviter_created_at = inviter_user.created_at

        created = False
        reason = "unknown"
        try:
            async with bot.async_session_factory() as session:
                created, reason = await register_referral_join(
                    session,
                    inviter_discord_id=inviter_id,
                    invitee_discord_id=member.id,
                    guild_id=guild.id,
                    invitee_account_created_at=member.created_at,
                    inviter_account_created_at=inviter_created_at,
                    is_first_guild_join=first_join,
                )
                await session.commit()
        except Exception:
            _LOG.exception(
                "referral join record failed inviter=%s invitee=%s guild=%s",
                inviter_id,
                member.id,
                guild.id,
            )
            return

        if created:
            _LOG.info(
                "Referral tracked: inviter=%s invitee=%s guild=%s code=%s",
                inviter_id,
                member.id,
                guild.id,
                used_code,
            )
        else:
            _LOG.info(
                "Referral skipped (%s): inviter=%s invitee=%s guild=%s code=%s",
                reason,
                inviter_id,
                member.id,
                guild.id,
                used_code,
            )

    bot.add_listener(_referral_on_ready, "on_ready")
    bot.add_listener(_referral_on_guild_join, "on_guild_join")
    bot.add_listener(_referral_on_invite_create, "on_invite_create")
    bot.add_listener(_referral_on_invite_delete, "on_invite_delete")
    bot.add_listener(_referral_on_member_join, "on_member_join")

    bot._pokepon_referral_listener = True  # type: ignore[attr-defined]
    _LOG.info("Guild referral listener enabled (invite → cd uses → crystal rewards).")


def sync_invite_tracker_uses(
    bot: commands.Bot, *, guild_id: int, invite_code: str, uses: int
) -> None:
    """Keep the in-memory invite cache in sync when the API mints a personal invite."""
    tracker: InviteTracker | None = getattr(bot, "invite_tracker", None)
    if tracker is not None:
        tracker.update_code(guild_id, invite_code, uses)
