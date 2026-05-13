"""Resolve the author of the message a command was posted as a reply to (chat input only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from discord.ext import commands


async def reply_target_user_id(bot: discord.Client, ctx: "commands.Context") -> int | None:
    """Discord user id of the replied-to message author, or ``None``.

    Slash-only invocations usually have no ``ctx.message``, so there is no reply chain — returns ``None``.
    Ignores replies to bots.
    """
    msg = ctx.message
    if msg is None or msg.reference is None:
        return None
    ref = msg.reference
    resolved = ref.resolved
    if isinstance(resolved, discord.Message):
        if resolved.author.bot:
            return None
        return resolved.author.id
    ch = ctx.channel
    if ch is None or ref.message_id is None:
        return None
    try:
        ref_msg = await ch.fetch_message(ref.message_id)
    except (discord.NotFound, discord.HTTPException):
        return None
    if ref_msg.author.bot:
        return None
    return ref_msg.author.id


async def resolve_collection_display_target(
    bot: discord.Client,
    ctx: "commands.Context",
    *,
    member_param: discord.Member | None,
) -> discord.User | discord.Member:
    """Explicit ``member`` wins; else reply author; else command author."""
    if member_param is not None:
        return member_param
    rid = await reply_target_user_id(bot, ctx)
    if rid is None:
        return ctx.author
    if ctx.guild is not None:
        m = ctx.guild.get_member(rid)
        if m is not None:
            return m
    try:
        return await bot.fetch_user(rid)
    except discord.NotFound:
        return ctx.author
