"""Developer-only utilities (Pokedollars, etc.); restricted by ``DEVELOPER_IDS`` in the environment."""

from __future__ import annotations

import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.config import Settings
from poke_pon_bot.services.rarity_luck_boost import (
    clear_rarity_luck_boost,
    format_stored_luck_summary,
    get_luck_boost_row,
    upsert_rarity_luck_boost,
)
from poke_pon_bot.services.cd_drop_themes import (
    clear_cd_drop_theme,
    format_stored_theme_summary,
    get_theme_row,
    upsert_cd_drop_theme,
    validate_theme_config,
)
from poke_pon_bot.services.crystals import (
    CRYSTAL_CURRENCY_NAME,
    CrystalsService,
    format_crystals,
)
from poke_pon_bot.services.discord_entitlement_admin import create_test_user_entitlement
from poke_pon_bot.services.catalog_card_ref import resolve_catalog_card_ref
from poke_pon_bot.services.drops import DropService
from poke_pon_bot.services.packs import (
    NoActiveSeriesError,
    PackService,
    UnknownSeriesError,
)
from poke_pon_bot.models.card_series import CardSeries
from poke_pon_bot.services.wallet import CURRENCY_NAME, WalletService, format_pokedollars
from sqlalchemy import select

_LOG = logging.getLogger(__name__)

_MAX_POKEDOLLARS = 2_147_483_647


class DevCog(commands.Cog):
    """Commands gated to user IDs in ``Settings.developer_ids``."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        s: object = getattr(bot, "settings", None)
        self._dev_ids: frozenset[int] = (
            s.developer_ids if isinstance(s, Settings) else frozenset()
        )
        self._settings: Settings | None = s if isinstance(s, Settings) else None
        self._wallet = WalletService()
        self._crystals = CrystalsService()

    def _is_dev(self, user_id: int) -> bool:
        return user_id in self._dev_ids

    dev = app_commands.Group(
        name="dev",
        description="Bot developer tools (set DEVELOPER_IDS in the environment).",
    )
    drop_theme = app_commands.Group(
        name="drop_theme",
        description="Global or server-wide /cd drop themes.",
        parent=dev,
    )
    luck_boost = app_commands.Group(
        name="luck_boost",
        description="Global or server-wide rarity luck (/cd, packs, wild duels).",
        parent=dev,
    )
    set_chase = app_commands.Group(
        name="set_chase",
        description="Test seasonal set chase community goal payout.",
        parent=dev,
    )
    event = app_commands.Group(
        name="event",
        description="Schedule in-game promos (luck, double daily, spotlight) without redeploy.",
        parent=dev,
    )

    async def _dev_denied(self, interaction: discord.Interaction) -> bool:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return True
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.",
                ephemeral=True,
            )
            return True
        return False

    def _theme_guild_for_scope(
        self,
        interaction: discord.Interaction,
        scope: str,
    ) -> int | None:
        if scope == "global":
            return None
        if scope == "server":
            if interaction.guild is None:
                raise ValueError("**server** scope must be run in the target Discord server.")
            return int(interaction.guild.id)
        raise ValueError("**scope** must be **global** or **server**.")

    @drop_theme.command(
        name="set",
        description="Set or update the /cd drop theme for global or this server.",
    )
    @app_commands.describe(
        scope="**global** = all servers · **server** = only where you run this command",
        kind="**series** = pack series code · **set** = one TCG set · **pokemon** = name contains",
        value="Series code (e.g. sv), set code (e.g. sv1), or Pokémon name (e.g. Pikachu)",
        chance_percent=(
            "**0** = theme never applies · **100** = every /cd card uses the theme · "
            "in between = per-card chance"
        ),
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Global (all servers)", value="global"),
            app_commands.Choice(name="This server only", value="server"),
        ],
        kind=[
            app_commands.Choice(name="Pack series (CardSeries code)", value="series"),
            app_commands.Choice(name="Single TCG set code", value="set"),
            app_commands.Choice(name="Pokémon name contains", value="pokemon"),
        ],
    )
    async def drop_theme_set(
        self,
        interaction: discord.Interaction,
        scope: str,
        kind: str,
        value: str,
        chance_percent: app_commands.Range[int, 0, 100],
    ) -> None:
        if await self._dev_denied(interaction):
            return
        try:
            guild_id = self._theme_guild_for_scope(interaction, scope)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        try:
            async with self.bot.async_session_factory() as session:
                constraints = await validate_theme_config(session, kind=kind, value=value)
                row = await upsert_cd_drop_theme(
                    session,
                    guild_id=guild_id,
                    kind=kind,
                    value=value.strip(),
                    chance_percent=int(chance_percent),
                    updated_by_discord_user_id=interaction.user.id,
                )
                await session.commit()
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except SQLAlchemyError:
            _LOG.exception("drop_theme set failed")
            await interaction.response.send_message(
                "Could not save the drop theme. Try again.",
                ephemeral=True,
            )
            return
        scope_name = "global" if guild_id is None else f"server `{guild_id}`"
        hint = ""
        if int(chance_percent) == 0:
            hint = "\n\n*At **0%** the theme is saved but will not affect `/cd` until you raise the chance.*"
        elif int(chance_percent) == 100:
            hint = "\n\n*At **100%** every card in each `/cd` pack will use this theme.*"
        detail = ""
        if constraints.set_codes:
            detail = f"\nSets: {', '.join(f'`{c}`' for c in constraints.set_codes[:6])}"
            if len(constraints.set_codes) > 6:
                detail += f" (+{len(constraints.set_codes) - 6} more)"
        elif constraints.name_contains:
            detail = f"\nName filter: **{constraints.name_contains}**"
        await interaction.response.send_message(
            f"Saved **{scope_name}** drop theme — **`{kind}`** `{value.strip()}` @ "
            f"**{int(chance_percent)}%** per card.{detail}{hint}",
            ephemeral=True,
        )

    @drop_theme.command(
        name="clear",
        description="Remove the global or server /cd drop theme.",
    )
    @app_commands.describe(
        scope="**global** or **server** (same as `/dev drop_theme set`)",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Global (all servers)", value="global"),
            app_commands.Choice(name="This server only", value="server"),
        ],
    )
    async def drop_theme_clear(
        self,
        interaction: discord.Interaction,
        scope: str,
    ) -> None:
        if await self._dev_denied(interaction):
            return
        try:
            guild_id = self._theme_guild_for_scope(interaction, scope)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        try:
            async with self.bot.async_session_factory() as session:
                removed = await clear_cd_drop_theme(session, guild_id=guild_id)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("drop_theme clear failed")
            await interaction.response.send_message(
                "Could not clear the drop theme. Try again.",
                ephemeral=True,
            )
            return
        scope_name = "global" if guild_id is None else f"server `{guild_id}`"
        if removed:
            await interaction.response.send_message(
                f"Cleared the **{scope_name}** drop theme.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"No **{scope_name}** drop theme was set.",
                ephemeral=True,
            )

    @drop_theme.command(
        name="show",
        description="Show the global and current-server /cd drop themes.",
    )
    async def drop_theme_show(self, interaction: discord.Interaction) -> None:
        if await self._dev_denied(interaction):
            return
        try:
            async with self.bot.async_session_factory() as session:
                global_row = await get_theme_row(session, guild_id=None)
                server_row = None
                if interaction.guild is not None:
                    server_row = await get_theme_row(session, guild_id=interaction.guild.id)
                global_line = await format_stored_theme_summary(
                    session, global_row, scope_name="Global",
                )
                if interaction.guild is not None:
                    server_line = await format_stored_theme_summary(
                        session,
                        server_row,
                        scope_name=f"Server (`{interaction.guild.id}`)",
                    )
                else:
                    server_line = "**Server:** *(run this in a server to view its theme)*"
        except SQLAlchemyError:
            _LOG.exception("drop_theme show failed")
            await interaction.response.send_message(
                "Could not load drop themes. Try again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "**`/cd` drop themes**\n\n"
            f"{global_line}\n"
            f"{server_line}\n\n"
            "_Server theme overrides global when both are set. "
            "Use `/dev drop_theme set` to configure.",
            ephemeral=True,
        )

    @luck_boost.command(
        name="set",
        description="Set rarity luck for /cd, booster packs, and wild /pd opponents.",
    )
    @app_commands.describe(
        scope="**global** or **server** (this server only)",
        luck_percent=(
            "**0** = normal · **+100** = much rarer · **-100** = much more common · "
            "beyond ±100 adds extra bias"
        ),
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Global (all servers)", value="global"),
            app_commands.Choice(name="This server only", value="server"),
        ],
    )
    async def luck_boost_set(
        self,
        interaction: discord.Interaction,
        scope: str,
        luck_percent: int,
    ) -> None:
        if await self._dev_denied(interaction):
            return
        try:
            guild_id = self._theme_guild_for_scope(interaction, scope)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        try:
            async with self.bot.async_session_factory() as session:
                await upsert_rarity_luck_boost(
                    session,
                    guild_id=guild_id,
                    luck_percent=int(luck_percent),
                    updated_by_discord_user_id=interaction.user.id,
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("luck_boost set failed")
            await interaction.response.send_message(
                "Could not save luck boost. Try again.",
                ephemeral=True,
            )
            return
        scope_name = "global" if guild_id is None else f"server `{guild_id}`"
        direction = (
            "rarer"
            if int(luck_percent) > 0
            else "more common"
            if int(luck_percent) < 0
            else "normal"
        )
        await interaction.response.send_message(
            f"Saved **{scope_name}** rarity luck: **{int(luck_percent):+d}%** "
            f"({direction} `/cd`, packs, wild `/pd`).",
            ephemeral=True,
        )

    @luck_boost.command(
        name="clear",
        description="Remove the global or server rarity luck boost.",
    )
    @app_commands.describe(scope="**global** or **server**")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Global (all servers)", value="global"),
            app_commands.Choice(name="This server only", value="server"),
        ],
    )
    async def luck_boost_clear(
        self,
        interaction: discord.Interaction,
        scope: str,
    ) -> None:
        if await self._dev_denied(interaction):
            return
        try:
            guild_id = self._theme_guild_for_scope(interaction, scope)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        try:
            async with self.bot.async_session_factory() as session:
                removed = await clear_rarity_luck_boost(session, guild_id=guild_id)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("luck_boost clear failed")
            await interaction.response.send_message(
                "Could not clear luck boost. Try again.",
                ephemeral=True,
            )
            return
        scope_name = "global" if guild_id is None else f"server `{guild_id}`"
        if removed:
            await interaction.response.send_message(
                f"Cleared **{scope_name}** rarity luck.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"No **{scope_name}** rarity luck was set.",
                ephemeral=True,
            )

    @luck_boost.command(
        name="show",
        description="Show global and server rarity luck boosts.",
    )
    async def luck_boost_show(self, interaction: discord.Interaction) -> None:
        if await self._dev_denied(interaction):
            return
        try:
            async with self.bot.async_session_factory() as session:
                global_row = await get_luck_boost_row(session, guild_id=None)
                server_row = None
                if interaction.guild is not None:
                    server_row = await get_luck_boost_row(
                        session, guild_id=interaction.guild.id
                    )
                global_line = await format_stored_luck_summary(
                    session, global_row, scope_name="Global"
                )
                if interaction.guild is not None:
                    server_line = await format_stored_luck_summary(
                        session,
                        server_row,
                        scope_name=f"Server (`{interaction.guild.id}`)",
                    )
                else:
                    server_line = "**Server:** *(run in a server to view its boost)*"
        except SQLAlchemyError:
            _LOG.exception("luck_boost show failed")
            await interaction.response.send_message(
                "Could not load luck boosts. Try again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "**Rarity luck boosts**\n\n"
            f"{global_line}\n"
            f"{server_line}\n\n"
            "_Server boost overrides global. Stacks with `/dev drop` luck on that command only._",
            ephemeral=True,
        )

    @set_chase.command(
        name="simulate",
        description="Fill the community bar and pay all registered participants.",
    )
    @app_commands.describe(
        user=(
            "Also register this user as a participant before payout; "
            "omit to only pay users who already claimed from the featured set."
        ),
        include_self="Register yourself as a participant before payout.",
    )
    async def set_chase_simulate(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        include_self: Literal["yes"] | None = None,
    ) -> None:
        if await self._dev_denied(interaction):
            return
        register_id: int | None = None
        if user is not None:
            register_id = int(user.id)
        elif include_self == "yes":
            register_id = int(interaction.user.id)
        from poke_pon_bot.services.set_chase import (
            build_status,
            dev_simulate_community_payout,
        )

        try:
            async with self.bot.async_session_factory() as session:
                season, paid, err = await dev_simulate_community_payout(
                    session,
                    register_user_id=register_id,
                )
                if err:
                    await interaction.response.send_message(err, ephemeral=True)
                    return
                assert season is not None
                amount = max(0, int(season.community_participation_crystals or 0))
                status = await build_status(
                    session, discord_user_id=interaction.user.id
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("dev set_chase simulate failed")
            await interaction.response.send_message(
                "Could not simulate community payout. Try again.",
                ephemeral=True,
            )
            return
        lines = [
            f"Simulated **{season.title}** community goal.",
            f"Bar set to **{int(season.global_target):,} / {int(season.global_target):,}**.",
            f"Paid **{paid}** participant(s) **{amount}** 💎 each.",
        ]
        if register_id is not None:
            who = user.mention if user is not None else "You"
            lines.append(f"Registered {who} as a participant before payout.")
        if status is not None and status.user_community_reward_paid:
            lines.append("✅ You received the community participation bonus.")
        elif status is not None and status.user_participated:
            lines.append("You were already registered and should have been paid.")
        elif register_id != int(interaction.user.id):
            lines.append(
                "_You were not registered — use **include_self: yes** or claim from the featured set first._"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @set_chase.command(
        name="reset",
        description="Clear community payout state so you can simulate again.",
    )
    @app_commands.describe(
        reset_claims="Also reset the community bar counter to 0.",
    )
    async def set_chase_reset(
        self,
        interaction: discord.Interaction,
        reset_claims: Literal["yes"] | None = None,
    ) -> None:
        if await self._dev_denied(interaction):
            return
        from poke_pon_bot.services.set_chase import dev_reset_community_payout

        try:
            async with self.bot.async_session_factory() as session:
                season, counts, err = await dev_reset_community_payout(
                    session,
                    reset_claims=reset_claims == "yes",
                )
                if err:
                    await interaction.response.send_message(err, ephemeral=True)
                    return
                assert season is not None
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("dev set_chase reset failed")
            await interaction.response.send_message(
                "Could not reset community payout. Try again.",
                ephemeral=True,
            )
            return
        claims_note = (
            f" Claims reset **{counts['claims_before']:,} → {counts['claims_after']:,}**."
            if reset_claims == "yes"
            else f" Bar left at **{counts['claims_after']:,} / {int(season.global_target):,}**."
        )
        await interaction.response.send_message(
            f"Reset **{season.title}** community payout state — "
            f"**{counts['participants_reset']}** participant payout flag(s) cleared.{claims_note} "
            "Run `/dev set_chase simulate` to test again.",
            ephemeral=True,
        )

    @set_chase.command(
        name="status",
        description="Show active set chase community bar and payout state.",
    )
    async def set_chase_status(self, interaction: discord.Interaction) -> None:
        if await self._dev_denied(interaction):
            return
        from poke_pon_bot.services.set_chase import build_status, format_set_chase_embed

        try:
            async with self.bot.async_session_factory() as session:
                status = await build_status(
                    session, discord_user_id=interaction.user.id
                )
        except SQLAlchemyError:
            _LOG.exception("dev set_chase status failed")
            await interaction.response.send_message(
                "Could not load set chase status. Try again.",
                ephemeral=True,
            )
            return
        if status is None:
            await interaction.response.send_message(
                "There is no active set chase right now.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            format_set_chase_embed(status),
            ephemeral=True,
        )

    @event.command(name="list", description="List scheduled game events.")
    async def event_list(self, interaction: discord.Interaction) -> None:
        if await self._dev_denied(interaction):
            return
        from poke_pon_bot.services.event_scheduler import event_status, fetch_enabled_events

        try:
            async with self.bot.async_session_factory() as session:
                rows = await fetch_enabled_events(session)
        except SQLAlchemyError:
            _LOG.exception("dev event list failed")
            await interaction.response.send_message("Could not load events.", ephemeral=True)
            return
        if not rows:
            await interaction.response.send_message("No scheduled game events.", ephemeral=True)
            return
        lines = []
        for row in rows:
            status = event_status(row)
            window = ""
            if row.recurrence:
                window = f" · `{row.recurrence}`"
            elif row.starts_at and row.ends_at:
                window = f" · `{row.starts_at.isoformat()}` → `{row.ends_at.isoformat()}`"
            lines.append(f"**#{row.id}** `{row.kind}` — **{row.title}** · *{status}*{window}")
        await interaction.response.send_message("\n".join(lines[:20]), ephemeral=True)

    @event.command(name="add", description="Create a one-shot scheduled game event.")
    @app_commands.describe(
        kind="Event type",
        title="Display name",
        starts="Start (ISO UTC, e.g. 2026-06-06T18:00:00+00:00)",
        ends="End (ISO UTC)",
        luck_percent="For luck_boost only",
        set_code="For set_spotlight only (TCG set code, e.g. sv1)",
        daily_multiplier="For double_daily (default 2)",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Rarity luck boost", value="luck_boost"),
            app_commands.Choice(name="Double daily rewards", value="double_daily"),
            app_commands.Choice(name="Free auction spotlight (fee holiday)", value="free_spotlight"),
            app_commands.Choice(name="Free spotlight for one set", value="set_spotlight"),
        ]
    )
    async def event_add(
        self,
        interaction: discord.Interaction,
        kind: str,
        title: str,
        starts: str,
        ends: str,
        luck_percent: app_commands.Range[int, -100, 500] | None = None,
        set_code: str | None = None,
        daily_multiplier: app_commands.Range[int, 2, 5] | None = None,
    ) -> None:
        if await self._dev_denied(interaction):
            return
        from poke_pon_bot.services.event_scheduler import create_event, parse_iso_datetime

        config: dict[str, object] = {}
        if kind == "luck_boost":
            config["luck_percent"] = int(luck_percent or 100)
        elif kind == "double_daily":
            config["multiplier"] = int(daily_multiplier or 2)
        elif kind == "set_spotlight":
            code = (set_code or "").strip().lower()
            if not code:
                await interaction.response.send_message(
                    "**set_code** is required for set_spotlight.",
                    ephemeral=True,
                )
                return
            config["set_code"] = code
        try:
            start_dt = parse_iso_datetime(starts)
            end_dt = parse_iso_datetime(ends)
            async with self.bot.async_session_factory() as session:
                row = await create_event(
                    session,
                    kind=kind,
                    title=title,
                    created_by_discord_user_id=interaction.user.id,
                    starts_at=start_dt,
                    ends_at=end_dt,
                    config=config,
                )
                await session.commit()
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except SQLAlchemyError:
            _LOG.exception("dev event add failed")
            await interaction.response.send_message("Could not save event.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Created game event **#{row.id}** `{row.kind}` — **{row.title}**.",
            ephemeral=True,
        )

    @event.command(name="cancel", description="Disable a scheduled game event by id.")
    @app_commands.describe(event_id="Event id from /dev event list")
    async def event_cancel(
        self,
        interaction: discord.Interaction,
        event_id: app_commands.Range[int, 1, 999_999],
    ) -> None:
        if await self._dev_denied(interaction):
            return
        from poke_pon_bot.services.event_scheduler import cancel_event

        try:
            async with self.bot.async_session_factory() as session:
                ok = await cancel_event(session, int(event_id))
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("dev event cancel failed")
            await interaction.response.send_message("Could not cancel event.", ephemeral=True)
            return
        if not ok:
            await interaction.response.send_message(
                f"No event with id **{event_id}**.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Event **#{event_id}** disabled.",
            ephemeral=True,
        )

    @event.command(
        name="template_weekend_luck",
        description="Create a recurring weekly weekend luck event (replaces env-only schedule).",
    )
    @app_commands.describe(
        luck_percent="Rarity luck percent during the window",
        timezone="IANA timezone for Fri 18:00 – Sun 20:00 window",
    )
    async def event_template_weekend_luck(
        self,
        interaction: discord.Interaction,
        luck_percent: app_commands.Range[int, 1, 500] = 100,
        timezone: str = "Europe/Stockholm",
    ) -> None:
        if await self._dev_denied(interaction):
            return
        from poke_pon_bot.models.scheduled_game_event import EVENT_KIND_LUCK_BOOST
        from poke_pon_bot.services.event_scheduler import (
            RECURRENCE_WEEKLY,
            create_event,
            default_weekend_luck_config,
        )

        try:
            async with self.bot.async_session_factory() as session:
                row = await create_event(
                    session,
                    kind=EVENT_KIND_LUCK_BOOST,
                    title=f"Weekend luck (+{int(luck_percent)}%)",
                    created_by_discord_user_id=interaction.user.id,
                    recurrence=RECURRENCE_WEEKLY,
                    config=default_weekend_luck_config(
                        luck_percent=int(luck_percent),
                        timezone=(timezone or "Europe/Stockholm").strip(),
                    ),
                )
                await session.commit()
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except SQLAlchemyError:
            _LOG.exception("dev event template_weekend_luck failed")
            await interaction.response.send_message("Could not save event.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Created recurring weekend luck event **#{row.id}** (`{timezone}`).",
            ephemeral=True,
        )

    @dev.command(
        name="pokedollars",
        description=f"Set a user’s {CURRENCY_NAME} balance (developers only).",
    )
    @app_commands.describe(
        amount="New balance (this replaces the current balance).",
        user="Whose balance to set; omit to set your own.",
    )
    async def dev_pokedollars(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 0, _MAX_POKEDOLLARS],
        user: discord.User | None = None,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set in the bot’s environment.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.",
                ephemeral=True,
            )
            return
        target = user or interaction.user
        try:
            async with self.bot.async_session_factory() as session:
                new_bal = await self._wallet.set_balance(session, target.id, int(amount))
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("dev set pokedollars for user %s", target.id)
            await interaction.response.send_message(
                "Could not save the balance. Try again.",
                ephemeral=True,
            )
            return
        if user is not None:
            line = f"Set **{target}**'s {CURRENCY_NAME} to **{format_pokedollars(new_bal)}**."
        else:
            line = f"Set your {CURRENCY_NAME} to **{format_pokedollars(new_bal)}**."
        await interaction.response.send_message(line, ephemeral=True)

    @dev.command(
        name="list_skus",
        description="List SKU ids for this bot application (use these for monetization / .env).",
    )
    async def dev_list_skus(self, interaction: discord.Interaction) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message("You don’t have access to **/dev** commands.", ephemeral=True)
            return
        try:
            skus = await interaction.client.fetch_skus()
        except discord.HTTPException as exc:
            _LOG.exception("dev list_skus")
            await interaction.response.send_message(
                f"Could not load SKUs: **{exc.status}** — {exc.text[:300] if exc.text else 'no body'}",
                ephemeral=True,
            )
            return
        if not skus:
            await interaction.response.send_message(
                "Discord returned **no SKUs** for this application. Create a monetization SKU in the Developer Portal first.",
                ephemeral=True,
            )
            return
        lines = [
            f"• **`{s.id}`** — **{s.name}** · type `{s.type.name}` · slug `{s.slug}` · "
            f"purchasable **{s.flags.available}** · flags=`{int(s.flags.value)}`"
            for s in skus
        ]
        body = "**SKUs for this bot token’s application** (use the **`id`** in `.env` or **`/dev grant_drop_boost_entitlement`**, not a store URL):\n" + "\n".join(
            lines,
        )
        if len(body) > 1900:
            body = body[:1897] + "…"
        await interaction.response.send_message(body, ephemeral=True)

    @dev.command(
        name="grant_drop_boost_entitlement",
        description="Create a test entitlement for the drop-boost SKU (developers only).",
    )
    @app_commands.describe(
        user="Who receives the test entitlement; omit for yourself.",
        sku_id="Optional. Paste the id from /dev list_skus as text (avoids slash INTEGER limits on large snowflakes).",
    )
    async def dev_grant_drop_boost_entitlement(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        sku_id: str | None = None,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message("You don’t have access to **/dev** commands.", ephemeral=True)
            return
        settings = self._settings
        resolved_sku: int | None = None
        if sku_id is not None and sku_id.strip():
            raw = sku_id.strip()
            if not raw.isdigit():
                await interaction.response.send_message(
                    "**`sku_id`** must be **digits only** (copy the number from **`/dev list_skus`**, no spaces or quotes).",
                    ephemeral=True,
                )
                return
            try:
                resolved_sku = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    "Could not parse **`sku_id`** as a numeric snowflake.",
                    ephemeral=True,
                )
                return
        if resolved_sku is None and settings is not None:
            resolved_sku = settings.discord_drop_boost_sku_id
        if resolved_sku is None:
            await interaction.response.send_message(
                "Pass **`sku_id`** or set **`DISCORD_DROP_BOOST_SKU_ID`**. "
                "Run **`/dev list_skus`** and copy the numeric **`id`** (not from a store URL).",
                ephemeral=True,
            )
            return
        target = user or interaction.user
        try:
            app_skus = await interaction.client.fetch_skus()
        except discord.HTTPException as exc:
            _LOG.exception("fetch_skus before grant")
            await interaction.response.send_message(
                f"Could not verify SKUs: **{exc.status}** — {exc.text[:300] if exc.text else 'no body'}",
                ephemeral=True,
            )
            return
        valid_ids = {s.id for s in app_skus}
        if resolved_sku not in valid_ids:
            await interaction.response.send_message(
                f"SKU **`{resolved_sku}`** is **not** listed for this bot’s application.\n"
                "Numbers copied from a **store or web URL** are often wrong or belong to another app.\n"
                "Run **`/dev list_skus`** and use one of the **`id`** values shown there (same app as **DISCORD_TOKEN**).",
                ephemeral=True,
            )
            return
        try:
            await create_test_user_entitlement(
                interaction.client,
                sku_id=int(resolved_sku),
                owner_user_id=int(target.id),
            )
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except discord.HTTPException as exc:
            _LOG.exception("create_entitlement sku=%s user=%s", resolved_sku, target.id)
            hint = ""
            if exc.status == 400:
                hint = (
                    "\n- Confirmed **`purchasable true`** on **`/dev list_skus`**? Draft SKUs often fail here.\n"
                    "- If the error persists, re-check **Developer Portal → Monetization** and Discord’s test-entitlement docs."
                )
            await interaction.response.send_message(
                f"Discord rejected the test entitlement: **{exc.status}** — {exc.text[:200] if exc.text else 'no body'}"
                f"{hint}",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Granted **test** drop-boost entitlement for SKU **`{resolved_sku}`** to {target.mention}.",
            ephemeral=True,
        )


    @dev.command(
        name="set_crystals",
        description=f"Set a user’s {CRYSTAL_CURRENCY_NAME} balance (developers only).",
    )
    @app_commands.describe(
        amount="New balance (this replaces the current balance).",
        user="Whose balance to set; omit to set your own.",
    )
    async def dev_set_crystals(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 0, _MAX_POKEDOLLARS],
        user: discord.User | None = None,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.", ephemeral=True
            )
            return
        target = user or interaction.user
        try:
            async with self.bot.async_session_factory() as session:
                new_bal = await self._crystals.set_balance(session, target.id, int(amount))
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("dev set_crystals for user %s", target.id)
            await interaction.response.send_message(
                "Could not save the balance. Try again.",
                ephemeral=True,
            )
            return
        if user is not None:
            line = (
                f"Set **{target}**'s {CRYSTAL_CURRENCY_NAME} to "
                f"**{format_crystals(new_bal)}**."
            )
        else:
            line = f"Set your {CRYSTAL_CURRENCY_NAME} to **{format_crystals(new_bal)}**."
        await interaction.response.send_message(line, ephemeral=True)

    @dev.command(
        name="drop",
        description="Open a /cd-style card drop with optional size, luck, and private mode.",
    )
    @app_commands.describe(
        private="Only you see the pack (same as /cd private).",
        card_count=(
            "Fixed number of cards in the pack (2–25). Omit for normal /cd (2 + random extras)."
        ),
        max_grabs=(
            "Max cards each user can claim from this drop (1–25; cannot exceed card_count)."
        ),
        runout_seconds=(
            "Seconds until the drop expires and unclaimed cards are gone (30–3600; default 180)."
        ),
        luck_percent="Extra rarity luck (stacks with server/global boost; can be below 0 or above 100).",
    )
    async def dev_drop(
        self,
        interaction: discord.Interaction,
        private: Literal["yes"] | None = None,
        card_count: app_commands.Range[int, 2, 25] | None = None,
        max_grabs: app_commands.Range[int, 1, 25] | None = None,
        runout_seconds: app_commands.Range[int, 30, 3600] | None = None,
        luck_percent: int = 0,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.",
                ephemeral=True,
            )
            return
        gacha = self.bot.cogs.get("GachaCog")
        if gacha is None or not hasattr(gacha, "run_card_drop"):
            await interaction.response.send_message(
                "Card drop is unavailable (gacha cog not loaded).",
                ephemeral=True,
            )
            return
        ctx = await self.bot.get_context(interaction)
        grabs = int(max_grabs) if max_grabs is not None else 1
        runout = int(runout_seconds) if runout_seconds is not None else 180
        if card_count is not None and grabs > int(card_count):
            await interaction.response.send_message(
                f"`max_grabs` cannot exceed `card_count` (**{int(card_count)}**).",
                ephemeral=True,
            )
            return

        header_bits = ["**Developer drop**"]
        if card_count is not None:
            header_bits.append(f"**{int(card_count)}** cards")
        if grabs > 1:
            header_bits.append(f"**{grabs}** grabs/user")
        if runout != 180:
            header_bits.append(f"**{runout}s** runout")
        if int(luck_percent) != 0:
            header_bits.append(f"**{int(luck_percent):+d}%** extra luck")
        if private == "yes":
            header_bits.append("**private**")
        header = " · ".join(header_bits)
        try:
            await gacha.run_card_drop(
                ctx,
                is_private=private == "yes",
                skip_cooldown=True,
                card_count=int(card_count) if card_count is not None else None,
                luck_percent=float(luck_percent),
                apply_drop_accounting=False,
                content_header=header,
                max_grabs_per_user=grabs,
                claim_seconds=runout,
            )
        except Exception:
            _LOG.exception("dev drop failed for user %s", interaction.user.id)
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Dev drop failed — check logs.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Dev drop failed — check logs.",
                    ephemeral=True,
                )

    @dev.command(
        name="sync_assemblies",
        description="Rebuild V-UNION assembly groups from the catalog (developers only).",
    )
    async def dev_sync_assemblies(self, interaction: discord.Interaction) -> None:
        if await self._dev_denied(interaction):
            return
        from poke_pon_bot.services.assembly_catalog import sync_all_assemblies

        try:
            async with self.bot.async_session_factory() as session:
                n = await sync_all_assemblies(session)
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("dev sync_assemblies failed")
            await interaction.response.send_message(
                "Could not sync assembly groups. Try again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Assembly registry synced — **{n}** group(s) upserted from catalog + YAML.",
            ephemeral=True,
        )

    @dev.command(
        name="grant_card",
        description="Grant one specific catalog card printing to a user (developers only).",
    )
    @app_commands.describe(
        card=(
            "Global catalog id: Pokémon TCG API ``tcg_card_id`` (e.g. ``swshp-SWSH159``, ``xy11-66``) "
            "or internal numeric catalog id."
        ),
        user="Who receives the card; omit to grant to yourself.",
    )
    async def dev_grant_card(
        self,
        interaction: discord.Interaction,
        card: str,
        user: discord.User | None = None,
    ) -> None:
        if await self._dev_denied(interaction):
            return
        ref = (card or "").strip()
        if not ref:
            await interaction.response.send_message(
                "Pass a **card** id (`tcg_card_id` from the catalog or a numeric catalog id).",
                ephemeral=True,
            )
            return
        target = user or interaction.user
        ds = DropService()
        try:
            async with self.bot.async_session_factory() as session:
                catalog_card = await resolve_catalog_card_ref(session, ref)
                if catalog_card is None:
                    await interaction.response.send_message(
                        f"No catalog card matches **`{ref}`**. "
                        "Use the Pokémon TCG API id (same as Pokédex / catalog sync), "
                        "or run catalog sync if the printing is missing.",
                        ephemeral=True,
                    )
                    return
                result = await ds.claim_card(
                    session,
                    discord_user_id=int(target.id),
                    card=catalog_card,
                    source="dev",
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("dev grant_card failed user=%s ref=%s", target.id, ref)
            await interaction.response.send_message(
                "Could not grant the card. Try again.",
                ephemeral=True,
            )
            return
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Granted **{catalog_card.name}** (`{catalog_card.tcg_card_id}`) to {target.mention}.\n"
            f"Card ID: `{result.public_id}` · set **{catalog_card.set_name}** "
            f"#{catalog_card.collector_number}",
            ephemeral=True,
        )

    @dev.command(
        name="grant_pack",
        description="Grant a pack to a user (random active series, or a specific series code).",
    )
    @app_commands.describe(
        user="Who receives the pack; omit to grant to yourself.",
        series_code="Optional series code (e.g. sv); omit for a random active series.",
    )
    async def dev_grant_pack(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        series_code: str | None = None,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.", ephemeral=True
            )
            return
        target = user or interaction.user
        ds = DropService()
        ps = PackService()
        try:
            async with self.bot.async_session_factory() as session:
                series_id: int | None = None
                if series_code:
                    series_row = await session.scalar(
                        select(CardSeries).where(CardSeries.code == series_code.strip())
                    )
                    if series_row is None:
                        await interaction.response.send_message(
                            f"No series with code `{series_code}`.", ephemeral=True
                        )
                        return
                    series_id = series_row.id
                pack = await ps.grant_dev_pack(
                    session,
                    ds,
                    discord_user_id=int(target.id),
                    series_id=series_id,
                    guild_id=interaction.guild_id,
                )
                await session.commit()
                series_row = await session.get(CardSeries, pack.series_id)
        except (NoActiveSeriesError, UnknownSeriesError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except SQLAlchemyError:
            _LOG.exception("dev grant_pack failed user=%s", target.id)
            await interaction.response.send_message(
                "Could not grant the pack. Try again.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Granted a **{series_row.display_name}** pack `{pack.public_id}` to {target.mention}.",
            ephemeral=True,
        )

    @dev.command(
        name="test_referral",
        description="Force-record a referral (bypasses inviter 7-day account-age check).",
    )
    @app_commands.describe(
        invitee="The 'referred friend' (their account joining your server).",
        inviter="The person who supposedly invited them; omit to use yourself.",
    )
    async def dev_test_referral(
        self,
        interaction: discord.Interaction,
        invitee: discord.User,
        inviter: discord.User | None = None,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.", ephemeral=True
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run this in a server (need a guild id to record the referral).",
                ephemeral=True,
            )
            return

        inviter_user = inviter or interaction.user
        if invitee.id == inviter_user.id:
            await interaction.response.send_message(
                "Invitee and inviter must be different users.", ephemeral=True
            )
            return

        from poke_pon_bot.services.referrals import (
            mark_first_guild_join,
            register_referral_join,
        )

        try:
            async with self.bot.async_session_factory() as session:
                await mark_first_guild_join(
                    session,
                    guild_id=int(interaction.guild_id),
                    discord_user_id=int(invitee.id),
                )
                # Re-check whether they are first-join after the upsert above.
                from poke_pon_bot.models.guild_member_seen import GuildMemberSeen

                seen_row = await session.get(
                    GuildMemberSeen, (int(interaction.guild_id), int(invitee.id))
                )
                # mark_first_guild_join only flags True the first time; force True here
                # so the dev command can create a referral row.
                created, reason = await register_referral_join(
                    session,
                    inviter_discord_id=int(inviter_user.id),
                    invitee_discord_id=int(invitee.id),
                    guild_id=int(interaction.guild_id),
                    invitee_account_created_at=invitee.created_at,
                    inviter_account_created_at=inviter_user.created_at,
                    is_first_guild_join=True,
                    skip_inviter_age_check=True,
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception(
                "dev test_referral failed inviter=%s invitee=%s",
                inviter_user.id,
                invitee.id,
            )
            await interaction.response.send_message(
                "Database error — check logs.", ephemeral=True
            )
            return

        if created:
            line = (
                f"Test referral recorded: {inviter_user.mention} → {invitee.mention} "
                f"in this server. Have **{invitee.mention}** run `/cd` 10 times "
                "to fire the reward DM."
            )
        else:
            human = {
                "self_invite": "inviter and invitee are the same user",
                "rejoin": "invitee already counted in this guild",
                "inviter_too_new": "inviter account too new (override didn’t apply)",
                "already_referred": "invitee already has a referral row "
                "(use `/dev reset_referral` first)",
            }.get(reason, reason)
            line = f"Referral **not** recorded — {human}."
        await interaction.response.send_message(line, ephemeral=True)

    @dev.command(
        name="reset_referral",
        description="Clear referral + first-join tracking for a user (testing alts).",
    )
    @app_commands.describe(
        user="The invitee to reset (their referral row and first-join flag).",
        guild_only="Only clear first-join in this server; omit to clear all servers.",
    )
    async def dev_reset_referral(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        guild_only: Literal["yes"] | None = None,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.", ephemeral=True
            )
            return
        if guild_only == "yes" and interaction.guild_id is None:
            await interaction.response.send_message(
                "Run this in a server when using **guild_only**.",
                ephemeral=True,
            )
            return

        from poke_pon_bot.services.referrals import reset_referral_test_state

        guild_id = int(interaction.guild_id) if guild_only == "yes" else None
        try:
            async with self.bot.async_session_factory() as session:
                counts = await reset_referral_test_state(
                    session,
                    discord_user_id=int(user.id),
                    guild_id=guild_id,
                )
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("dev reset_referral failed user=%s", user.id)
            await interaction.response.send_message(
                "Database error — check logs.", ephemeral=True
            )
            return

        scope = f"guild `{guild_id}`" if guild_id is not None else "all guilds"
        await interaction.response.send_message(
            f"Reset referral test state for {user.mention} (`{user.id}`) — "
            f"referral row removed: **{counts['referral_deleted']}**, "
            f"first-join cleared ({scope}): **{counts['seen_deleted']}**. "
            "They can join again via a personal invite link to re-test.",
            ephemeral=True,
        )

    @dev.command(
        name="catalog_news_sync",
        description="Run incremental TCG catalog sync and post any new-set announcements.",
    )
    async def dev_catalog_news_sync(self, interaction: discord.Interaction) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.", ephemeral=True
            )
            return
        if not self._settings or not self._settings.catalog_news_enabled:
            await interaction.response.send_message(
                "Catalog news is disabled — set **`CATALOG_NEWS_ENABLED=1`**.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        cog = self.bot.get_cog("CatalogNewsCog")
        if cog is None:
            from poke_pon_bot.services.catalog_news import _site_origin, run_catalog_news_sync

            try:
                result = await run_catalog_news_sync(
                    self.bot,
                    self.bot.async_session_factory,
                    api_key=self._settings.tcg_api_key,
                    site_origin=_site_origin(self._settings.web_frontend_url),
                    lookback_days=self._settings.catalog_news_lookback_days,
                    min_cards_existing_set=self._settings.catalog_news_min_cards_existing_set,
                    max_sets_per_run=self._settings.catalog_news_max_sets_per_run,
                    discord_channel_id=self._settings.catalog_news_channel_id,
                    post_discord=self._settings.catalog_news_channel_id is not None,
                )
                lines = result.messages or ["Done."]
            except SQLAlchemyError:
                _LOG.exception("dev catalog_news_sync failed")
                await interaction.followup.send("Database error — check logs.", ephemeral=True)
                return
        else:
            try:
                lines = await cog.run_once()
            except SQLAlchemyError:
                _LOG.exception("dev catalog_news_sync failed")
                await interaction.followup.send("Database error — check logs.", ephemeral=True)
                return
        body = "\n".join(f"• {line}" for line in lines[:12])
        await interaction.followup.send(body or "Catalog sync finished.", ephemeral=True)

    @dev.command(
        name="catalog_news_seed",
        description="Insert a [TEST] catalog news item for website/Discord smoke tests (staging only).",
    )
    @app_commands.describe(
        kind="Announcement type to simulate",
        set_code="Real set code for Pokédex/Pack links (default sv2)",
        post_to_discord="Post embed to CATALOG_NEWS_CHANNEL_ID when set",
        clear_previous="Delete prior dev-seed test rows first",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="New set", value="new_set"),
            app_commands.Choice(name="Cards added", value="cards_added"),
        ]
    )
    async def dev_catalog_news_seed(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        set_code: str | None = None,
        post_to_discord: bool = True,
        clear_previous: bool = False,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.", ephemeral=True
            )
            return
        if not self._settings or self._settings.pokepon_runtime != "staging":
            await interaction.response.send_message(
                "**/dev catalog_news_seed** is staging-only — use the staging bot.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        from poke_pon_bot.models.catalog_announcement import (
            ANNOUNCE_KIND_CARDS_ADDED,
            ANNOUNCE_KIND_NEW_SET,
        )
        from poke_pon_bot.services.catalog_news import (
            _site_origin,
            seed_test_catalog_announcement,
        )

        kind_value = kind.value
        if kind_value == "new_set":
            announce_kind = ANNOUNCE_KIND_NEW_SET
        elif kind_value == "cards_added":
            announce_kind = ANNOUNCE_KIND_CARDS_ADDED
        else:
            await interaction.followup.send(f"Unknown kind: `{kind_value}`.", ephemeral=True)
            return

        channel_id = self._settings.catalog_news_channel_id
        do_discord = bool(post_to_discord and channel_id is not None)

        try:
            result = await seed_test_catalog_announcement(
                self.bot,
                self.bot.async_session_factory,
                site_origin=_site_origin(self._settings.web_frontend_url),
                kind=announce_kind,
                set_code=(set_code.strip().lower() if set_code else None),
                post_discord=do_discord,
                discord_channel_id=channel_id,
                clear_previous=clear_previous,
            )
        except SQLAlchemyError:
            _LOG.exception("dev catalog_news_seed failed")
            await interaction.followup.send("Database error — check logs.", ephemeral=True)
            return
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        lines = [
            f"Created **[TEST]** announcement **#{result.announcement_id}** — {result.title}.",
            f"API: `{self._settings.web_public_url.rstrip('/')}/api/news`",
            f"Site: {_site_origin(self._settings.web_frontend_url)}/",
        ]
        if clear_previous and result.cleared_previous:
            lines.append(f"Cleared **{result.cleared_previous}** prior dev-seed row(s).")
        if post_to_discord and channel_id is None:
            lines.append("Discord skipped — set **`CATALOG_NEWS_CHANNEL_ID`** in `.env.staging`.")
        elif do_discord:
            lines.append(
                "Discord embed posted."
                if result.discord_posted
                else "Discord post failed — check channel ID and bot permissions."
            )
        elif not post_to_discord:
            lines.append("Discord post skipped (`post_to_discord=false`).")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @dev.command(
        name="reset_tutorial",
        description="Clear tutorial progress (and tutorial packs) for testing.",
    )
    @app_commands.describe(
        user="Discord user to reset",
        remove_member_role="Also remove the tutorial Member role in the main server",
    )
    async def dev_reset_tutorial(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        remove_member_role: bool = False,
    ) -> None:
        if not self._dev_ids:
            await interaction.response.send_message(
                "Developer commands are disabled until **`DEVELOPER_IDS`** is set.",
                ephemeral=True,
            )
            return
        if not self._is_dev(interaction.user.id):
            await interaction.response.send_message(
                "You don’t have access to **/dev** commands.", ephemeral=True
            )
            return

        from poke_pon_bot.services.tutorial import reset_tutorial_for_testing

        try:
            counts = await reset_tutorial_for_testing(
                self.bot,
                self.bot.async_session_factory,
                discord_user_id=int(user.id),
                remove_member_role=remove_member_role,
            )
        except SQLAlchemyError:
            _LOG.exception("dev reset_tutorial failed user=%s", user.id)
            await interaction.response.send_message(
                "Database error — check logs.", ephemeral=True
            )
            return

        role_note = (
            " Member role removed in the main server."
            if remove_member_role
            else " Member role was not changed — set **remove_member_role** if needed."
        )
        await interaction.response.send_message(
            f"Reset tutorial for {user.mention} (`{user.id}`) — "
            f"progress row deleted: **{counts['tutorial_row_deleted']}**, "
            f"tutorial pack(s) removed: **{counts['tutorial_packs_deleted']}**.{role_note} "
            "They can use **Verify** or **`/tutorial`** again.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DevCog(bot))
