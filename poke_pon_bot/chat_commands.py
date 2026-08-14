"""PokePon chat command aliases: ``pp`` + slash name (e.g. ``ppdaily`` for ``/daily``).

Slash commands use Discord's ``/`` menu. Everywhere else, message commands must start with
``pp``. In the **official server** (``TUTORIAL_GUILD_ID``), bare names like ``balance`` or
``deck edit`` also work.

Short forms (``ppbal``, ``ppas``, …) are listed in ``PP_BARE_SHORTCUTS`` and registered as
command aliases via ``pp_chat_aliases()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext import commands

PP_PREFIX = "pp"

# After ``pp``, map one token → command name (no spaces).
PP_BARE_SHORTCUTS: dict[str, str] = {
    "bal": "balance",
    "day": "daily",
    "dboost": "drop_boost",
    "db": "drop_boost",
    "evol": "cevolve",
    "ev": "cevolve",
    "gr": "grade",
    "tut": "tutorial",
    "mis": "marketinveststatus",
    "kcol": "packcolv",
    "packc": "packcolv",
    "pcat": "packcat",
    "pvd": "packv",
}

# After ``pp``, map one token → ``group sub`` (two tokens).
PP_COMPOUND_SHORTCUTS: dict[str, tuple[str, str]] = {
    "as": ("auction", "search"),
    "ahs": ("auction", "search"),
    "ac": ("auction", "create"),
    "ab": ("auction", "bid"),
    "asp": ("auction", "spotlight"),
    "to": ("trade", "offer"),
    "tg": ("trade", "gift"),
    "de": ("deck", "edit"),
    "dv": ("deck", "view"),
    "dc": ("duel", "challenge"),
}


def pp_alias(command_name: str) -> str:
    """Chat alias for a slash command or group (``cd`` → ``ppcd``, ``packcolv`` → ``pppackcolv``)."""
    return f"{PP_PREFIX}{command_name}"


def pp_aliases(*command_names: str) -> list[str]:
    return [pp_alias(n) for n in command_names]


def pp_chat_aliases(command_name: str, *short_suffixes: str) -> list[str]:
    """Full ``pp<name>`` plus optional short ``pp<suffix>`` forms (e.g. ``bal`` → ``ppbal``)."""
    seen: set[str] = set()
    out: list[str] = []

    def add(name: str) -> None:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append(name)

    add(pp_alias(command_name))
    for suffix in short_suffixes:
        token = suffix.strip().lower()
        if token:
            add(f"{PP_PREFIX}{token}")
    return out


def expand_pp_bare_tokens(parts: list[str]) -> list[str]:
    """Expand ``ppbal``-style first tokens into real command names for dispatch."""
    if not parts:
        return parts
    head = parts[0].lower()
    if head in PP_BARE_SHORTCUTS:
        return [PP_BARE_SHORTCUTS[head], *parts[1:]]
    if head in PP_COMPOUND_SHORTCUTS:
        a, b = PP_COMPOUND_SHORTCUTS[head]
        return [a, b, *parts[1:]]
    return parts


def is_pp_chat_token(token: str) -> bool:
    t = token.lower()
    return t.startswith(PP_PREFIX) and len(t) > len(PP_PREFIX)


def is_official_guild(guild_id: int | None, official_guild_id: int | None) -> bool:
    return (
        guild_id is not None
        and official_guild_id is not None
        and guild_id == official_guild_id
    )


def strip_pp_prefix(content: str) -> str | None:
    """``ppcd g pikachu`` → ``cd g pikachu``; ``ppbal`` → ``balance``; returns ``None`` if not ``pp``."""
    parts = content.split()
    if not parts or not is_pp_chat_token(parts[0]):
        return None
    bare = parts[0][len(PP_PREFIX) :]
    expanded = expand_pp_bare_tokens([bare, *parts[1:]])
    return " ".join(expanded)


def _registered_command_token_count(bot: commands.Bot, parts: list[str]) -> int:
    if not parts:
        return 0
    first = parts[0].lower()
    if is_pp_chat_token(first):
        bare = first[len(PP_PREFIX) :]
        expanded = expand_pp_bare_tokens([bare, *parts[1:]])
        first = expanded[0].lower()
        parts = expanded
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
