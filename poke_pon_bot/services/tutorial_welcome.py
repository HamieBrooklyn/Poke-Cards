"""Welcome-channel verification panel (persistent button → DM tutorial)."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from poke_pon_bot.config import Settings
from poke_pon_bot.services.tutorial import (
    grant_member_role,
    is_tutorial_complete,
    send_current_step_dm,
    start_tutorial,
    tutorial_enabled,
)

_LOG = logging.getLogger(__name__)

VERIFY_BUTTON_CUSTOM_ID = "pokepon:tutorial_verify"
VERIFY_PANEL_EMBED_TITLE = "PokePon — Get access"


def pre_member_channel_mentions(settings: Settings) -> str:
    if not settings.tutorial_pre_member_channel_ids:
        return ""
    return " ".join(f"<#{cid}>" for cid in sorted(settings.tutorial_pre_member_channel_ids))


def build_verify_panel_embed(settings: Settings) -> discord.Embed:
    mentions = pre_member_channel_mentions(settings)
    lines = [
        "Welcome! Before you can see the rest of the server, complete a **short interactive tutorial** in your DMs.",
        "",
        "1. Press **Verify** below (or run **`/tutorial`**).",
        "2. Follow the steps in your **DM** — each step includes a **copy & paste** command.",
        "3. When you finish, you receive the **Member** role and can see all channels.",
    ]
    if mentions:
        lines.extend(
            [
                "",
                f"Until then, you only have access to: {mentions}",
            ]
        )
    embed = discord.Embed(
        title=VERIFY_PANEL_EMBED_TITLE,
        description="\n".join(lines),
        colour=discord.Colour.green(),
    )
    embed.set_footer(text="Tutorial can only be completed once per account.")
    return embed


class TutorialVerifyView(discord.ui.View):
    """Persistent view for the welcome-channel verify button."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self._bot = bot

    @discord.ui.button(
        label="Verify",
        style=discord.ButtonStyle.success,
        custom_id=VERIFY_BUTTON_CUSTOM_ID,
        emoji="✅",
    )
    async def verify(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        settings: Settings = self._bot.settings
        if interaction.user is None or interaction.user.bot:
            return
        if not tutorial_enabled(settings):
            await interaction.response.send_message(
                "Verification is not available right now.",
                ephemeral=True,
            )
            return
        uid = interaction.user.id
        guild_id = interaction.guild.id if interaction.guild else settings.tutorial_guild_id

        if await is_tutorial_complete(self._bot.async_session_factory, uid):
            await grant_member_role(
                self._bot,
                settings,
                discord_user_id=uid,
                guild_id=guild_id,
            )
            await interaction.response.send_message(
                "You have already completed verification — the **Member** role should be on your account now. "
                "If it is still missing, ask a moderator to check the bot's **Manage Roles** permission.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Starting your tutorial — **check your DMs** from me in a moment. "
            "If nothing arrives, enable DMs from server members and press **Verify** again, "
            "or run **`/tutorial`**.",
            ephemeral=True,
        )

        await start_tutorial(
            self._bot.async_session_factory,
            discord_user_id=uid,
            guild_id=guild_id,
        )
        await send_current_step_dm(self._bot, uid)


def register_verify_view(bot: commands.Bot) -> None:
    bot.add_view(TutorialVerifyView(bot))


async def ensure_verify_panel(bot: commands.Bot) -> None:
    """Post or refresh the welcome-channel verify panel in the main tutorial guild."""
    settings = bot.settings
    if not tutorial_enabled(settings) or settings.tutorial_verify_channel_id is None:
        return
    guild = bot.get_guild(int(settings.tutorial_guild_id))
    if guild is None:
        return
    channel = guild.get_channel(int(settings.tutorial_verify_channel_id))
    if not isinstance(channel, discord.TextChannel):
        try:
            channel = await bot.fetch_channel(int(settings.tutorial_verify_channel_id))
        except (discord.NotFound, discord.HTTPException):
            _LOG.warning("Tutorial verify channel %s not found", settings.tutorial_verify_channel_id)
            return
    if not isinstance(channel, discord.TextChannel):
        return
    me = guild.me
    if me is None or not channel.permissions_for(me).send_messages:
        _LOG.warning("Cannot send verify panel in channel %s", channel.id)
        return

    embed = build_verify_panel_embed(settings)
    view = TutorialVerifyView(bot)

    existing: discord.Message | None = None
    if settings.tutorial_verify_panel_message_id is not None:
        try:
            existing = await channel.fetch_message(settings.tutorial_verify_panel_message_id)
        except (discord.NotFound, discord.HTTPException):
            existing = None

    if existing is None:
        try:
            async for msg in channel.history(limit=25):
                if msg.author.id != bot.user.id:
                    continue
                if msg.embeds and msg.embeds[0].title == VERIFY_PANEL_EMBED_TITLE:
                    existing = msg
                    break
        except discord.Forbidden:
            existing = None

    try:
        if existing is not None:
            await existing.edit(embed=embed, view=view)
            _LOG.info("Updated verify panel in #%s (%s)", channel.name, channel.id)
        else:
            msg = await channel.send(embed=embed, view=view)
            try:
                await msg.pin()
            except discord.HTTPException:
                pass
            _LOG.info(
                "Posted verify panel in #%s (%s) — message id %s (optional: TUTORIAL_VERIFY_PANEL_MESSAGE_ID=%s)",
                channel.name,
                channel.id,
                msg.id,
                msg.id,
            )
    except discord.Forbidden:
        _LOG.warning("Forbidden posting verify panel in channel %s", channel.id)
