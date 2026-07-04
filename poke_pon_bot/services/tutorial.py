"""Interactive DM tutorial: 5-step quest chain with crystal rewards."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import discord
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.chat_commands import pp_alias
from poke_pon_bot.config import Settings
from poke_pon_bot.models.user_tutorial import UserTutorial
from poke_pon_bot.services.crystals import CrystalsService, format_crystals

_LOG = logging.getLogger(__name__)

STEP_WELCOME: Final = "welcome"
STEP_BALANCE: Final = "balance"
STEP_DAILY: Final = "daily"
STEP_DROP: Final = "drop"
STEP_COLLECTION: Final = "collection"
STEP_PACK: Final = "pack"
STEP_DONE: Final = "done"

QUEST_STEPS: tuple[str, ...] = (
    STEP_BALANCE,
    STEP_DAILY,
    STEP_DROP,
    STEP_COLLECTION,
    STEP_PACK,
)

ORDERED_STEPS: tuple[str, ...] = (STEP_WELCOME, *QUEST_STEPS, STEP_DONE)

STEP_CRYSTAL_REWARDS: dict[str, int] = {
    STEP_BALANCE: 5,
    STEP_DAILY: 5,
    STEP_DROP: 10,
    STEP_COLLECTION: 10,
    STEP_PACK: 10,
}

TOTAL_QUEST_CRYSTALS = sum(STEP_CRYSTAL_REWARDS.values())

POKEPON_COLLECTION_URL = "https://pokepon.org/collection/"
POKEPON_DECK_URL = "https://pokepon.org/deck/"

# Map legacy 8-step tutorial progress onto the new chain.
_LEGACY_STEP_MAP: dict[str, str] = {
    "view_global": STEP_COLLECTION,
    "view_collection": STEP_COLLECTION,
    "browse": STEP_PACK,
    "deck": STEP_PACK,
    "evolve": STEP_PACK,
    "pack": STEP_PACK,
    "vote": STEP_PACK,
}


@dataclass(frozen=True)
class StepContent:
    title: str
    body: str
    try_hint: str
    copy_example: str = ""
    crystal_reward: int = 0


def _reward_suffix(crystals: int) -> str:
    if crystals <= 0:
        return ""
    return f" · {format_crystals(crystals)}"


STEP_CONTENT: dict[str, StepContent] = {
    STEP_WELCOME: StepContent(
        title="Welcome to PokePon",
        body=(
            f"**5 quests** → Member role + up to **{format_crystals(TOTAL_QUEST_CRYSTALS)}**.\n"
            "Use Discord's **`/`** menu for each step."
        ),
        try_hint="Press **Start tutorial**.",
    ),
    STEP_BALANCE: StepContent(
        title="1/5 — Balance",
        body=f"Check your wallet{_reward_suffix(STEP_CRYSTAL_REWARDS[STEP_BALANCE])}.",
        try_hint="",
        copy_example="/balance",
        crystal_reward=STEP_CRYSTAL_REWARDS[STEP_BALANCE],
    ),
    STEP_DAILY: StepContent(
        title="2/5 — Daily",
        body=f"Claim your daily reward{_reward_suffix(STEP_CRYSTAL_REWARDS[STEP_DAILY])}.",
        try_hint="",
        copy_example="/daily",
        crystal_reward=STEP_CRYSTAL_REWARDS[STEP_DAILY],
    ),
    STEP_DROP: StepContent(
        title="3/5 — Card drop",
        body=f"Open a drop and pick a card{_reward_suffix(STEP_CRYSTAL_REWARDS[STEP_DROP])}.",
        try_hint="I'll continue when you claim a card.",
        copy_example="/cd",
        crystal_reward=STEP_CRYSTAL_REWARDS[STEP_DROP],
    ),
    STEP_COLLECTION: StepContent(
        title="4/5 — Collection",
        body=f"View your cards{_reward_suffix(STEP_CRYSTAL_REWARDS[STEP_COLLECTION])}.",
        try_hint="",
        copy_example="/colv",
        crystal_reward=STEP_CRYSTAL_REWARDS[STEP_COLLECTION],
    ),
    STEP_PACK: StepContent(
        title="5/5 — Open a pack",
        body=(
            f"A free pack is waiting for you{_reward_suffix(STEP_CRYSTAL_REWARDS[STEP_PACK])}. "
            "Tap **Open** in the browser."
        ),
        try_hint="I'll continue when the pack is opened.",
        copy_example="/packcolv",
        crystal_reward=STEP_CRYSTAL_REWARDS[STEP_PACK],
    ),
    STEP_DONE: StepContent(
        title="Done!",
        body="You're in. Have fun collecting!",
        try_hint="",
    ),
}


def tutorial_enabled(settings: Settings) -> bool:
    return settings.tutorial_enabled and settings.tutorial_guild_id is not None


def is_main_tutorial_guild(guild_id: int | None, settings: Settings) -> bool:
    return guild_id is not None and settings.tutorial_guild_id == guild_id


def normalize_tutorial_step(step: str) -> str:
    if step in ORDERED_STEPS:
        return step
    if step == "drop":
        return STEP_DROP
    if step == "daily":
        return STEP_DAILY
    return _LEGACY_STEP_MAP.get(step, STEP_BALANCE)


def quest_step_index(step: str) -> int | None:
    if step not in QUEST_STEPS:
        return None
    return QUEST_STEPS.index(step) + 1


async def get_tutorial_row(
    session: AsyncSession,
    discord_user_id: int,
) -> UserTutorial | None:
    row = await session.get(UserTutorial, discord_user_id)
    if row is None:
        return None
    normalized = normalize_tutorial_step(row.current_step)
    if normalized != row.current_step:
        row.current_step = normalized
        await session.flush()
    return row


async def is_tutorial_complete(
    session_factory: async_sessionmaker[AsyncSession],
    discord_user_id: int,
) -> bool:
    async with session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        return row is not None and row.completed_at is not None


async def start_tutorial(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    discord_user_id: int,
    guild_id: int | None,
) -> UserTutorial:
    async with session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        now = datetime.now(UTC)
        if row is None:
            row = UserTutorial(
                discord_user_id=discord_user_id,
                current_step=STEP_WELCOME,
                started_at=now,
                completed_at=None,
                guild_id=guild_id,
                crystals_earned=0,
            )
            session.add(row)
        elif row.completed_at is not None:
            return row
        else:
            if guild_id is not None:
                row.guild_id = guild_id
        await session.commit()
        await session.refresh(row)
        return row


def _leaf_command(ctx: commands.Context) -> commands.Command[Any, Any, Any] | None:
    if ctx.command is None:
        return None
    cmd = ctx.command
    wrapped = getattr(cmd, "wrapped", None)
    if wrapped is not None:
        return wrapped
    return cmd


def _command_name(ctx: commands.Context) -> str:
    cmd = _leaf_command(ctx)
    if cmd is None:
        return ""
    return (cmd.qualified_name or cmd.name or "").lower()


def _command_aliases(ctx: commands.Context) -> set[str]:
    names: set[str] = set()
    cmd = _leaf_command(ctx)
    if cmd is not None:
        names.add(cmd.name.lower())
        qn = (cmd.qualified_name or "").lower()
        if qn:
            names.add(qn)
        for alias in cmd.aliases or []:
            names.add(alias.lower())
    if ctx.invoked_with:
        names.add(ctx.invoked_with.lower().split()[0])
    return names


def _arg_scope(ctx: commands.Context) -> str | None:
    scope = ctx.kwargs.get("scope")
    if scope is not None:
        return str(scope).lower()
    interaction = ctx.interaction
    if interaction is not None and interaction.namespace is not None:
        raw = getattr(interaction.namespace, "scope", None)
        if raw is not None:
            if hasattr(raw, "value"):
                return str(raw.value).lower()
            return str(raw).lower()
    if ctx.args:
        for a in ctx.args:
            if isinstance(a, str) and a.lower() in ("g", "c"):
                return a.lower()
    if ctx.message and ctx.message.content:
        parts = ctx.message.content.strip().split()
        if len(parts) >= 2 and parts[0].lstrip("/").lower() in _command_aliases(ctx):
            token = parts[1].lower()
            if token in ("g", "c"):
                return token
    return None


def command_satisfies_step(step: str, ctx: commands.Context) -> bool:
    names = _command_aliases(ctx)
    qname = _command_name(ctx)
    if step == STEP_BALANCE:
        return bool(names & {"balance", "pcbal", pp_alias("balance")})
    if step == STEP_DAILY:
        return bool(names & {"daily", pp_alias("daily")})
    if step == STEP_DROP:
        return False
    if step == STEP_COLLECTION:
        if names & {"colv", pp_alias("colv"), "coll", pp_alias("coll")}:
            return True
        if "cv" in names and _arg_scope(ctx) == "c":
            return True
    if step == STEP_PACK:
        return False
    return False


def next_step(current: str) -> str | None:
    current = normalize_tutorial_step(current)
    try:
        idx = ORDERED_STEPS.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(ORDERED_STEPS):
        return None
    return ORDERED_STEPS[idx + 1]


async def _grant_step_crystals(
    session: AsyncSession,
    row: UserTutorial,
    completed_step: str,
) -> int:
    amount = STEP_CRYSTAL_REWARDS.get(completed_step, 0)
    if amount <= 0:
        return 0
    await CrystalsService().try_credit(session, row.discord_user_id, amount)
    row.crystals_earned = int(row.crystals_earned or 0) + amount
    return amount


async def try_advance_step(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    discord_user_id: int,
    force_from_step: str | None = None,
) -> bool:
    """Move to the next step if the user is on ``force_from_step`` (or current)."""
    async with session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        if row is None or row.completed_at is not None:
            return False
        current = normalize_tutorial_step(row.current_step)
        if force_from_step is not None and current != normalize_tutorial_step(force_from_step):
            return False
        prev_step = current
        nxt = next_step(prev_step)
        if nxt is None:
            return False
        await _grant_step_crystals(session, row, prev_step)
        row.current_step = nxt
        if nxt == STEP_DONE:
            row.completed_at = datetime.now(UTC)
        await session.commit()
    return True


async def complete_tutorial(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    discord_user_id: int,
) -> UserTutorial | None:
    async with session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        if row is None:
            return None
        if row.completed_at is None:
            if normalize_tutorial_step(row.current_step) == STEP_PACK:
                await _grant_step_crystals(session, row, STEP_PACK)
            row.current_step = STEP_DONE
            row.completed_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
        return row


def resolve_tutorial_guild_id(
    guild_id: int | None,
    settings: Settings,
) -> int | None:
    if guild_id is not None:
        return guild_id
    return settings.tutorial_guild_id


async def grant_member_role(
    bot: discord.Client,
    settings: Settings,
    *,
    discord_user_id: int,
    guild_id: int | None,
) -> None:
    guild_id = resolve_tutorial_guild_id(guild_id, settings)
    if guild_id is None or settings.tutorial_member_role_id is None:
        _LOG.warning(
            "Tutorial role not granted for user %s — missing guild_id or TUTORIAL_MEMBER_ROLE_ID",
            discord_user_id,
        )
        return
    if not is_main_tutorial_guild(guild_id, settings):
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except (discord.NotFound, discord.HTTPException):
            _LOG.warning("Tutorial guild %s not found for role grant", guild_id)
            return
    role = guild.get_role(settings.tutorial_member_role_id)
    if role is None:
        _LOG.warning("Tutorial Member role %s missing in guild %s", settings.tutorial_member_role_id, guild_id)
        return
    member = guild.get_member(discord_user_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_user_id)
        except (discord.NotFound, discord.HTTPException):
            _LOG.info("User %s not in guild %s — role granted when they rejoin", discord_user_id, guild_id)
            return
    if role in member.roles:
        return
    me = guild.me
    if me is None:
        _LOG.warning("Bot member missing in guild %s — cannot assign Member role", guild_id)
        return
    if not me.guild_permissions.manage_roles:
        _LOG.warning(
            "Cannot assign Member role in guild %s — bot needs **Manage Roles** permission",
            guild_id,
        )
        return
    if role >= me.top_role:
        _LOG.warning(
            "Cannot assign Member role in guild %s — drag the bot's role **above** the Member role "
            "in Server Settings → Roles",
            guild_id,
        )
        return
    try:
        await member.add_roles(role, reason="Completed PokePon tutorial")
        _LOG.info("Granted Member role to user %s in guild %s", discord_user_id, guild_id)
    except discord.Forbidden:
        _LOG.warning(
            "Forbidden adding Member role for user %s in guild %s — check Manage Roles and role order",
            discord_user_id,
            guild_id,
        )
    except discord.HTTPException:
        _LOG.exception("Failed to add Member role for user %s", discord_user_id)


def _pre_member_channels_line(settings: Settings) -> str:
    ids = settings.tutorial_pre_member_channel_ids
    if not ids:
        return ""
    mentions = " ".join(f"<#{cid}>" for cid in sorted(ids))
    return f"\n\nUntil you finish, you only have access to: {mentions}"


def build_completion_embed(settings: Settings, *, crystals_earned: int) -> discord.Embed:
    return discord.Embed(
        title="Tutorial complete",
        description=(
            f"**Member** role unlocked · earned **{format_crystals(crystals_earned)}**.\n"
            f"[Collection]({POKEPON_COLLECTION_URL}) · [Deck]({POKEPON_DECK_URL})"
        ),
        colour=discord.Colour.gold(),
    )


def build_step_embed(step: str, settings: Settings) -> discord.Embed:
    content = STEP_CONTENT.get(step, STEP_CONTENT[STEP_WELCOME])
    body = content.body
    if step == STEP_WELCOME:
        body += _pre_member_channels_line(settings)
    if content.try_hint:
        body = f"{body}\n\n_{content.try_hint}_"
    embed = discord.Embed(
        title=content.title,
        description=body,
        colour=discord.Colour.blurple(),
    )
    if content.copy_example:
        embed.add_field(name="Run", value=f"`{content.copy_example}`", inline=False)
    idx = quest_step_index(step)
    if idx is not None:
        embed.set_footer(text=f"Quest {idx} of {len(QUEST_STEPS)} · up to {format_crystals(TOTAL_QUEST_CRYSTALS)} total")
    return embed


class TutorialNavView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: commands.Bot,
        owner_id: int,
        step: str,
    ) -> None:
        super().__init__(timeout=3600)
        self._bot = bot
        self._owner_id = owner_id
        self._step = step
        if step == STEP_WELCOME:
            self.add_item(TutorialStartButton(bot=bot, owner_id=owner_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is None or interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This tutorial is not for you.",
                ephemeral=True,
            )
            return False
        return True


class TutorialStartButton(discord.ui.Button):
    def __init__(self, *, bot: commands.Bot, owner_id: int) -> None:
        super().__init__(label="Start tutorial", style=discord.ButtonStyle.primary)
        self._bot = bot
        self._owner_id = owner_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await advance_and_notify(self._bot, self._owner_id)


async def ensure_tutorial_pack_granted(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    discord_user_id: int,
) -> None:
    from poke_pon_bot.models.pack_instance import UserPackInstance
    from poke_pon_bot.services.drops import DropService
    from poke_pon_bot.services.packs import PackService
    from sqlalchemy import select

    async with session_factory() as session:
        existing = await session.scalar(
            select(UserPackInstance.id).where(
                UserPackInstance.discord_user_id == discord_user_id,
                UserPackInstance.source == "tutorial",
                UserPackInstance.opened_at.is_(None),
            )
        )
        if existing is not None:
            return
        await PackService().grant_tutorial_pack(
            session,
            DropService(),
            discord_user_id=discord_user_id,
        )
        await session.commit()


async def send_current_step_dm(bot: commands.Bot, discord_user_id: int) -> None:
    settings: Settings = bot.settings
    crystals_earned = 0
    async with bot.async_session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        if row is None:
            return
        step = normalize_tutorial_step(row.current_step)
        crystals_earned = int(row.crystals_earned or 0)
        if row.completed_at is not None:
            step = STEP_DONE
    if step == STEP_PACK:
        await ensure_tutorial_pack_granted(
            bot.async_session_factory,
            discord_user_id=discord_user_id,
        )

    try:
        user = await bot.fetch_user(discord_user_id)
    except (discord.NotFound, discord.HTTPException):
        return

    if step == STEP_DONE:
        embed = build_completion_embed(settings, crystals_earned=crystals_earned)
        view = None
        async with bot.async_session_factory() as session:
            row = await get_tutorial_row(session, discord_user_id)
        if row:
            await grant_member_role(
                bot,
                settings,
                discord_user_id=discord_user_id,
                guild_id=row.guild_id,
            )
    else:
        embed = build_step_embed(step, settings)
        view = (
            TutorialNavView(bot=bot, owner_id=discord_user_id, step=step)
            if step == STEP_WELCOME
            else None
        )

    try:
        await user.send(embed=embed, view=view)
    except discord.Forbidden:
        _LOG.info("Tutorial DM blocked for user %s", discord_user_id)
    except discord.HTTPException:
        _LOG.exception("Tutorial DM failed for user %s", discord_user_id)


async def _after_step_advanced(bot: commands.Bot, discord_user_id: int) -> None:
    settings: Settings = bot.settings
    async with bot.async_session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        if row and row.completed_at is not None:
            await grant_member_role(
                bot,
                settings,
                discord_user_id=discord_user_id,
                guild_id=row.guild_id,
            )
    await send_current_step_dm(bot, discord_user_id)


async def advance_and_notify(bot: commands.Bot, discord_user_id: int) -> None:
    if await try_advance_step(
        bot.async_session_factory,
        discord_user_id=discord_user_id,
    ):
        await _after_step_advanced(bot, discord_user_id)


async def handle_command_for_tutorial(bot: commands.Bot, ctx: commands.Context) -> None:
    if ctx.author.bot or ctx.command is None:
        return
    settings: Settings = bot.settings
    if not tutorial_enabled(settings):
        return
    uid = ctx.author.id
    async with bot.async_session_factory() as session:
        row = await get_tutorial_row(session, uid)
        if row is None or row.completed_at is not None:
            return
        step = normalize_tutorial_step(row.current_step)
    if step == STEP_WELCOME:
        return
    if step in (STEP_DROP, STEP_PACK):
        return
    if not command_satisfies_step(step, ctx):
        return
    await advance_and_notify(bot, uid)


async def notify_drop_claimed_for_tutorial(bot: commands.Bot, discord_user_id: int) -> None:
    """Advance the drop quest only after the user claims a card from a /cd pack."""
    if await try_advance_step(
        bot.async_session_factory,
        discord_user_id=discord_user_id,
        force_from_step=STEP_DROP,
    ):
        await _after_step_advanced(bot, discord_user_id)


async def notify_pack_opened_for_tutorial(bot: commands.Bot, discord_user_id: int) -> None:
    """Advance the pack quest only after the user opens a booster pack."""
    if await try_advance_step(
        bot.async_session_factory,
        discord_user_id=discord_user_id,
        force_from_step=STEP_PACK,
    ):
        await _after_step_advanced(bot, discord_user_id)


async def reset_tutorial_for_testing(
    bot: commands.Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    discord_user_id: int,
    remove_member_role: bool = False,
) -> dict[str, int]:
    """Delete tutorial progress so the user can run onboarding again."""
    from poke_pon_bot.models.pack_instance import UserPackInstance
    from sqlalchemy import delete

    counts = {"tutorial_row_deleted": 0, "tutorial_packs_deleted": 0}
    async with session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        guild_id = row.guild_id if row is not None else None
        if row is not None:
            await session.delete(row)
            counts["tutorial_row_deleted"] = 1
        pack_result = await session.execute(
            delete(UserPackInstance).where(
                UserPackInstance.discord_user_id == discord_user_id,
                UserPackInstance.source == "tutorial",
            )
        )
        counts["tutorial_packs_deleted"] = int(pack_result.rowcount or 0)
        await session.commit()

    if remove_member_role:
        settings: Settings = bot.settings  # type: ignore[attr-defined]
        if guild_id is not None and settings.tutorial_member_role_id is not None:
            await _try_remove_member_role(bot, settings, discord_user_id, guild_id)

    return counts


async def _try_remove_member_role(
    bot: discord.Client,
    settings: Settings,
    discord_user_id: int,
    guild_id: int,
) -> None:
    if not is_main_tutorial_guild(guild_id, settings) or settings.tutorial_member_role_id is None:
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except (discord.NotFound, discord.HTTPException):
            return
    role = guild.get_role(settings.tutorial_member_role_id)
    if role is None:
        return
    member = guild.get_member(discord_user_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_user_id)
        except (discord.NotFound, discord.HTTPException):
            return
    if role not in member.roles:
        return
    try:
        await member.remove_roles(role, reason="Tutorial reset (developer testing)")
    except (discord.Forbidden, discord.HTTPException):
        _LOG.warning(
            "Could not remove Member role for user %s in guild %s during tutorial reset",
            discord_user_id,
            guild_id,
        )
