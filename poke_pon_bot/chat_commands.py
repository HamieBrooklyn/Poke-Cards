"""PokePon chat command aliases: ``pp`` + slash name (e.g. ``ppdaily`` for ``/daily``).

Slash commands use Discord's ``/`` menu. Everywhere else, message commands must start with
``pp``. In the **official server** (``TUTORIAL_GUILD_ID``), bare names like ``balance`` or
``deck edit`` also work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext import commands

PP_PREFIX = "pp"


def pp_alias(command_name: str) -> str:
    """Chat alias for a slash command or group (``cd`` → ``ppcd``, ``packcolv`` → ``pppackcolv``)."""
    return f"{PP_PREFIX}{command_name}"


def pp_aliases(*command_names: str) -> list[str]:
    return [pp_alias(n) for n in command_names]


def is_pp_chat_token(token: str) -> bool:
    t = token.lower()
    return t.startswith(PP_PREFIX) and len(t) > len(PP_PREFIX)


def is_official_guild(guild_id: int | None, official_guild_id: int | None) -> bool:
    return (
        guild_id is not None
        and official_guild_id is not None
        and guild_id == official_guild_id
    )


def _registered_command_token_count(bot: commands.Bot, parts: list[str]) -> int:
    if not parts:
        return 0
    first = parts[0].lower()
    if bot.get_command(first) is not None:
        return 1
    if len(parts) >= 2:
        combined = f"{first} {parts[1].lower()}"
        if bot.get_command(combined) is not None:
            return 2
    return 0


def chat_command_token_count(
    bot: commands.Bot,
    content: str,
    *,
    allow_bare_names: bool = False,
) -> int:
    """How many leading tokens form a registered chat command (1 or 2 for groups)."""
    parts = content.split()
    if not parts:
        return 0
    first = parts[0].lower()
    if is_pp_chat_token(first):
        return _registered_command_token_count(bot, parts)
    if allow_bare_names:
        return _registered_command_token_count(bot, parts)
    return 0
