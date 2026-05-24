"""Interactive DM tutorial: step tracking, command checkpoints, Member role on completion."""

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
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.services.packs import PackService

_LOG = logging.getLogger(__name__)

TUTORIAL_EVOLVE_STEP_REWARD = 2000
POKEPON_WEBSITE_URL = "https://pokepon.org"
POKEPON_COLLECTION_URL = "https://pokepon.org/collection/"
POKEPON_DECK_URL = "https://pokepon.org/deck/"

STEP_WELCOME: Final = "welcome"
STEP_DROP: Final = "drop"
STEP_VIEW_GLOBAL: Final = "view_global"
STEP_VIEW_COLLECTION: Final = "view_collection"
STEP_DECK: Final = "deck"
STEP_EVOLVE: Final = "evolve"
STEP_PACK: Final = "pack"
STEP_DAILY: Final = "daily"
STEP_VOTE: Final = "vote"
STEP_DONE: Final = "done"

ORDERED_STEPS: tuple[str, ...] = (
    STEP_WELCOME,
    STEP_DROP,
    STEP_VIEW_GLOBAL,
    STEP_VIEW_COLLECTION,
    STEP_DECK,
    STEP_EVOLVE,
    STEP_PACK,
    STEP_DAILY,
    STEP_VOTE,
    STEP_DONE,
)


@dataclass(frozen=True)
class StepContent:
    title: str
    body: str
    try_hint: str
    #: One-line chat command players can copy (``pp`` prefix works in every server).
    copy_example: str = ""


def _pp_chat_display(slash_or_chat: str) -> str:
    parts = slash_or_chat.lstrip("/").strip().split()
    if not parts:
        return pp_alias("help")
    head = pp_alias(parts[0])
    if len(parts) > 1:
        return f"{head} {' '.join(parts[1:])}"
    return head


def _cmd_help(slash: str, *, chat: str | None = None, extra: str = "") -> str:
    """Prefer slash (autocomplete); chat uses the ``pp`` prefix."""
    chat_display = _pp_chat_display(chat or slash)
    line = (
        f"Use **{slash}** — open Discord's **`/`** menu for autocomplete. "
        f"Without a slash, type **`{chat_display}`** (not the bare command name)."
    )
    return f"{line}\n{extra}" if extra else line


STEP_CONTENT: dict[str, StepContent] = {
    STEP_WELCOME: StepContent(
        title="Welcome to PokePon",
        body=(
            "This short tutorial walks you through the basics in **your DMs** "
            "while you try each feature yourself.\n\n"
            "We recommend **slash commands** (`/…`) for autocomplete. In chat, use the **`pp`** "
            "prefix (e.g. `ppcd`, `ppdaily`) — bare names like `cd` do not work.\n\n"
            "Complete it once to unlock the server channels."
        ),
        try_hint="Press **Start tutorial** below.",
    ),
    STEP_DROP: StepContent(
        title="Step 1 — Card drop",
        body=(
            _cmd_help("/cd", chat="cd")
            + "\n\nWorks here in DMs or in the server. You will see a pack collage — "
            "**pick one card** to keep."
        ),
        try_hint="Run the command above and claim a card — I will continue automatically.",
        copy_example=pp_alias("cd"),
    ),
    STEP_VIEW_GLOBAL: StepContent(
        title="Step 2 — Browse the catalog",
        body=(
            _cmd_help(
                "/cv",
                extra="Slash: **scope** = **Global catalog (g)**, **name** = `Pikachu` (or any Pokémon).",
            )
            + "\n\nShows catalog art for matching printings."
        ),
        try_hint="Run the copy-paste line (or fill **/cv** the same way).",
        copy_example=f"{pp_alias('cv')} g pikachu",
    ),
    STEP_VIEW_COLLECTION: StepContent(
        title="Step 3 — Your collection",
        body=(
            _cmd_help("/colv", chat="colv")
            + f"\n\nText list instead: `{pp_alias('coll')}`."
            + f"\nOne owned copy with art: `{pp_alias('cv')} c` + your **Card ID** from **colv**."
        ),
        try_hint="Run **colv** (or **coll**) from the step above.",
        copy_example=pp_alias("colv"),
    ),
    STEP_DECK: StepContent(
        title="Step 4 — Duel deck",
        body=(
            _cmd_help("/deck edit", chat="deck edit")
            + f"\n\nView saved bench: `{pp_alias('deck view')}`."
        ),
        try_hint="Run **deck edit**, pick a slot, then reply with a **Card ID** from your collection.",
        copy_example=pp_alias("deck edit"),
    ),
    STEP_EVOLVE: StepContent(
        title="Step 5 — Evolve a card",
        body=(
            _cmd_help("/cevolve", chat="cevolve")
            + "\n\nReplace `YOUR-CARD-ID` in the copy-paste line with the id from **colv** / **cv c**. "
            f"Reward when you finish: **₽2,000**.\n\n"
            "No eligible card yet? Press **Skip**."
        ),
        try_hint="Paste your real Card ID, or press **Skip**.",
        copy_example=f"{pp_alias('cevolve')} YOUR-CARD-ID",
    ),
    STEP_PACK: StepContent(
        title="Step 6 — Open your free pack",
        body=(
            "I added a **free booster pack** to your account.\n\n"
            + _cmd_help("/packcolv", chat="packcolv")
            + "\n\nFind the tutorial pack and press **Open** on it."
        ),
        try_hint="Run **packcolv** and open the tutorial pack.",
        copy_example=pp_alias("packcolv"),
    ),
    STEP_DAILY: StepContent(
        title="Step 7 — Daily reward",
        body=(
            _cmd_help("/daily", chat="daily")
            + "\n\nClaim free Pokedollars once per UTC day."
        ),
        try_hint="Run **daily** once.",
        copy_example=pp_alias("daily"),
    ),
    STEP_VOTE: StepContent(
        title="Step 8 — Vote for the bot",
        body=(
            _cmd_help("/vote", chat="vote")
            + "\n\nVote on Top.gg — rewards are granted automatically after you vote.\n\n"
            "**Already voted for PokePon before?** Run **/vote** once — if your vote is "
            "already on file, we'll skip this step and finish the tutorial."
        ),
        try_hint="Run **vote** once.",
        copy_example=pp_alias("vote"),
    ),
    STEP_DONE: StepContent(
        title="Tutorial complete",
        body=(
            "You are all set. Have fun collecting, trading, and dueling!"
        ),
        try_hint="",
    ),
}


def tutorial_enabled(settings: Settings) -> bool:
    return settings.tutorial_enabled and settings.tutorial_guild_id is not None


def is_main_tutorial_guild(guild_id: int | None, settings: Settings) -> bool:
    return guild_id is not None and settings.tutorial_guild_id == guild_id


async def get_tutorial_row(
    session: AsyncSession,
    discord_user_id: int,
) -> UserTutorial | None:
    return await session.get(UserTutorial, discord_user_id)


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
    """Names that may refer to the invoked command (incl. chat aliases)."""
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
    if step == STEP_DROP:
        return False
    if step == STEP_VIEW_GLOBAL and "cv" in names and _arg_scope(ctx) == "g":
        return True
    if step == STEP_VIEW_COLLECTION:
        if names & {"colv", pp_alias("colv"), "coll", pp_alias("coll")}:
            return True
        if "cv" in names and _arg_scope(ctx) == "c":
            return True
    if step == STEP_DECK and (
        qname == "deck"
        or qname.startswith("deck ")
        or qname == pp_alias("deck")
        or qname.startswith(f"{pp_alias('deck')} ")
    ):
        return True
    if step == STEP_EVOLVE and ("cevolve" in names or pp_alias("cevolve") in names):
        return True
    if step == STEP_PACK and names & {"packcolv", pp_alias("packcolv")}:
        return True
    if step == STEP_DAILY and names & {"daily", pp_alias("daily")}:
        return True
    if step == STEP_VOTE and names & {"vote", pp_alias("vote")}:
        return True
    return False


def next_step(current: str) -> str | None:
    try:
        idx = ORDERED_STEPS.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(ORDERED_STEPS):
        return None
    return ORDERED_STEPS[idx + 1]


async def vote_step_already_satisfied(
    session: AsyncSession,
    settings: Settings,
    discord_user_id: int,
) -> bool:
    """True if the user has already voted and received (or is in) a rewarded vote window."""
    from poke_pon_bot.services.topgg_vote import (
        TopggAuthError,
        TopggRateLimitError,
        fetch_active_discord_vote,
    )
    from poke_pon_bot.services.wallet import WalletService, _same_topgg_vote_slice

    wallet = WalletService()
    row = await wallet._get_or_create(session, discord_user_id)
    if row.last_vote_claim_at is not None:
        return True
    token = settings.topgg_api_token
    if not token:
        return False
    try:
        status = await fetch_active_discord_vote(token, discord_user_id)
    except (TopggAuthError, TopggRateLimitError):
        return False
    except Exception:
        _LOG.debug("Tutorial vote check failed for user %s", discord_user_id, exc_info=True)
        return False
    if status is None:
        return False
    return _same_topgg_vote_slice(row.last_rewarded_topgg_vote_at, status.created_at)


async def try_complete_vote_step_if_eligible(
    bot: commands.Bot | discord.Client,
    discord_user_id: int,
) -> bool:
    """
    Skip the vote tutorial step when the user already voted (reward claimed or active window paid).

    Attempts a Top.gg auto-claim first so a fresh vote during the tutorial still counts.
    """
    settings: Settings = bot.settings  # type: ignore[attr-defined]
    session_factory = bot.async_session_factory  # type: ignore[attr-defined]
    async with session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        if row is None or row.completed_at is not None or row.current_step != STEP_VOTE:
            return False

    if settings.topgg_api_token:
        from poke_pon_bot.services.topgg_auto_claim import claim_active_topgg_vote

        await claim_active_topgg_vote(session_factory, settings, discord_user_id)

    async with session_factory() as session:
        if not await vote_step_already_satisfied(session, settings, discord_user_id):
            return False

    if await try_advance_step(
        session_factory,
        discord_user_id=discord_user_id,
        force_from_step=STEP_VOTE,
    ):
        await _after_step_advanced(bot, discord_user_id)
        return True
    return False


async def grant_tutorial_pack(
    session_factory: async_sessionmaker[AsyncSession],
    discord_user_id: int,
) -> None:
    drop = DropService()
    packs = PackService()
    async with session_factory() as session:
        from poke_pon_bot.models.pack_instance import UserPackInstance
        from sqlalchemy import select

        existing = await session.execute(
            select(UserPackInstance.id)
            .where(
                UserPackInstance.discord_user_id == discord_user_id,
                UserPackInstance.source == "tutorial",
            )
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return
        inst = await packs.grant_tutorial_pack(
            session,
            drop,
            discord_user_id=discord_user_id,
        )
        await session.commit()
        _LOG.info("Tutorial pack granted user=%s pack=%s", discord_user_id, inst.public_id)


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
        if force_from_step is not None and row.current_step != force_from_step:
            return False
        prev_step = row.current_step
        nxt = next_step(prev_step)
        if nxt is None:
            return False
        if prev_step == STEP_EVOLVE:
            from poke_pon_bot.services.wallet import WalletService

            await WalletService().try_credit(
                session,
                discord_user_id,
                TUTORIAL_EVOLVE_STEP_REWARD,
            )
        row.current_step = nxt
        if nxt == STEP_DONE:
            row.completed_at = datetime.now(UTC)
        await session.commit()
    if nxt == STEP_PACK:
        await grant_tutorial_pack(session_factory, discord_user_id)
    return True


async def skip_evolve_step(
    session_factory: async_sessionmaker[AsyncSession],
    discord_user_id: int,
) -> bool:
    async with session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        if row is None or row.completed_at is not None or row.current_step != STEP_EVOLVE:
            return False
    return await try_advance_step(session_factory, discord_user_id=discord_user_id)


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
            row.current_step = STEP_DONE
            row.completed_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
        return row


def resolve_tutorial_guild_id(
    guild_id: int | None,
    settings: Settings,
) -> int | None:
    """Guild to grant Member in — row value, else configured main server."""
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


def build_completion_embed(settings: Settings) -> discord.Embed:
    """Final message after the last tutorial step (includes website)."""
    embed = discord.Embed(
        title="Tutorial complete — you're in!",
        description=(
            "You should now have the **Member** role and access to the full server.\n\n"
            "For an easier time browsing your binder, trading, auctions, and editing your duel deck, "
            f"check out the website:\n"
            f"**{POKEPON_WEBSITE_URL}**"
        ),
        colour=discord.Colour.gold(),
    )
    embed.add_field(
        name="Handy links",
        value=(
            f"[Collection]({POKEPON_COLLECTION_URL}) · "
            f"[Deck editor]({POKEPON_DECK_URL}) · "
            f"[Shop]({POKEPON_WEBSITE_URL}/shop/)"
        ),
        inline=False,
    )
    embed.set_footer(text="Thanks for playing PokePon!")
    return embed


def build_pack_opened_embed() -> discord.Embed:
    """Sent right after the tutorial pack is opened (before daily/vote steps)."""
    return discord.Embed(
        title="Pack opened — great job!",
        description=(
            "Your free tutorial pack is done. **Two quick steps left** (daily + vote), "
            "then verification is complete.\n\n"
            f"When you're fully done, visit **{POKEPON_WEBSITE_URL}** — "
            "the collection and deck tools are often easier on the web."
        ),
        colour=discord.Colour.green(),
    )


async def send_pack_opened_milestone_dm(bot: commands.Bot, discord_user_id: int) -> None:
    try:
        user = await bot.fetch_user(discord_user_id)
        await user.send(embed=build_pack_opened_embed())
    except discord.Forbidden:
        _LOG.info("Pack milestone DM blocked for user %s", discord_user_id)
    except discord.HTTPException:
        _LOG.exception("Pack milestone DM failed for user %s", discord_user_id)


def build_step_embed(step: str, settings: Settings) -> discord.Embed:
    content = STEP_CONTENT.get(step, STEP_CONTENT[STEP_WELCOME])
    body = content.body
    if step == STEP_WELCOME:
        body += _pre_member_channels_line(settings)
    embed = discord.Embed(
        title=content.title,
        description=body,
        colour=discord.Colour.blurple(),
    )
    if content.copy_example:
        embed.add_field(
            name="Copy & paste",
            value=f"`{content.copy_example}`",
            inline=False,
        )
    if content.try_hint:
        embed.add_field(name="Your turn", value=content.try_hint, inline=False)
    if step == STEP_VOTE and settings.topgg_vote_url:
        embed.add_field(
            name="Vote link",
            value=settings.topgg_vote_url,
            inline=False,
        )
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
        elif step == STEP_EVOLVE:
            self.add_item(TutorialSkipEvolveButton(bot=bot, owner_id=owner_id))

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


class TutorialSkipEvolveButton(discord.ui.Button):
    def __init__(self, *, bot: commands.Bot, owner_id: int) -> None:
        super().__init__(label="Skip evolve step", style=discord.ButtonStyle.secondary)
        self._bot = bot
        self._owner_id = owner_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if await skip_evolve_step(self._bot.async_session_factory, self._owner_id):
            await _after_step_advanced(self._bot, self._owner_id)


async def send_current_step_dm(bot: commands.Bot, discord_user_id: int) -> None:
    settings: Settings = bot.settings
    async with bot.async_session_factory() as session:
        row = await get_tutorial_row(session, discord_user_id)
        if row is None:
            return
        step = row.current_step
        if row.completed_at is not None:
            step = STEP_DONE

    if step == STEP_VOTE:
        if await try_complete_vote_step_if_eligible(bot, discord_user_id):
            return

    if step == STEP_PACK:
        await grant_tutorial_pack(bot.async_session_factory, discord_user_id)

    try:
        user = await bot.fetch_user(discord_user_id)
    except (discord.NotFound, discord.HTTPException):
        return

    if step == STEP_DONE:
        embed = build_completion_embed(settings)
        view = None
        row = None
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
        if step == STEP_PACK:
            from poke_pon_bot.services.wallet import format_pokedollars

            embed.add_field(
                name="Evolve step bonus",
                value=f"You received **{format_pokedollars(TUTORIAL_EVOLVE_STEP_REWARD)}** "
                "for completing the evolve step.",
                inline=False,
            )
        view = TutorialNavView(bot=bot, owner_id=discord_user_id, step=step) if step in (
            STEP_WELCOME,
            STEP_EVOLVE,
        ) else None

    try:
        await user.send(embed=embed, view=view)
    except discord.Forbidden:
        _LOG.info("Tutorial DM blocked for user %s", discord_user_id)
    except discord.HTTPException:
        _LOG.exception("Tutorial DM failed for user %s", discord_user_id)


async def _after_step_advanced(bot: commands.Bot, discord_user_id: int) -> None:
    if await try_complete_vote_step_if_eligible(bot, discord_user_id):
        return
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


async def notify_pack_opened(bot: commands.Bot, discord_user_id: int, *, pack_source: str) -> None:
    if pack_source != "tutorial":
        return
    if await try_advance_step(
        bot.async_session_factory,
        discord_user_id=discord_user_id,
        force_from_step=STEP_PACK,
    ):
        await send_pack_opened_milestone_dm(bot, discord_user_id)
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
        step = row.current_step
    if step == STEP_WELCOME:
        return
    if step == STEP_VOTE:
        if await try_complete_vote_step_if_eligible(bot, uid):
            return
    if not command_satisfies_step(step, ctx):
        return
    if step == STEP_PACK:
        return
    if step == STEP_DROP:
        return
    await advance_and_notify(bot, uid)


async def notify_drop_claimed_for_tutorial(bot: commands.Bot, discord_user_id: int) -> None:
    """Advance the drop step only after the user claims a card from a /cd pack."""
    if await try_advance_step(
        bot.async_session_factory,
        discord_user_id=discord_user_id,
        force_from_step=STEP_DROP,
    ):
        await _after_step_advanced(bot, discord_user_id)


async def reset_tutorial_for_testing(
    bot: commands.Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    discord_user_id: int,
    remove_member_role: bool = False,
) -> dict[str, int]:
    """Delete tutorial progress (and tutorial packs) so the user can run onboarding again."""
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
