"""Developer-only utilities (Pokedollars, etc.); restricted by ``DEVELOPER_IDS`` in the environment."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.config import Settings
from poke_pon_bot.services.crystals import (
    CRYSTAL_CURRENCY_NAME,
    CrystalsService,
    format_crystals,
)
from poke_pon_bot.services.discord_entitlement_admin import create_test_user_entitlement
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DevCog(bot))
