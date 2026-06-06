"""Shared Discord UI for catalog wishlist toggles."""

from __future__ import annotations

import logging

import discord
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.wishlist import MAX_WISHLIST_ENTRIES, add_wishlist, remove_wishlist

_LOG = logging.getLogger(__name__)


class WishlistToggleButton(discord.ui.Button):
    """Star button that toggles the current card on/off the viewer's wishlist."""

    _STAR = "⭐"

    def __init__(
        self,
        *,
        session_factory,
        card_id: int,
        viewer_id: int,
        wishlisted: bool,
        row: int = 0,
    ) -> None:
        style = discord.ButtonStyle.primary if wishlisted else discord.ButtonStyle.secondary
        super().__init__(emoji=self._STAR, style=style, row=row)
        self._session_factory = session_factory
        self._card_id = card_id
        self._viewer_id = viewer_id
        self._wishlisted = wishlisted

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this view can wishlist.",
                ephemeral=True,
            )
            return
        try:
            async with self._session_factory() as session:
                if self._wishlisted:
                    await remove_wishlist(
                        session, discord_user_id=self._viewer_id, card_id=self._card_id
                    )
                    self._wishlisted = False
                else:
                    added = await add_wishlist(
                        session, discord_user_id=self._viewer_id, card_id=self._card_id
                    )
                    if not added:
                        await interaction.response.send_message(
                            f"Wishlist is full (max {MAX_WISHLIST_ENTRIES} cards) or already wishlisted.",
                            ephemeral=True,
                        )
                        return
                    self._wishlisted = True
                await session.commit()
        except SQLAlchemyError:
            _LOG.exception("wishlist toggle card_id=%s user=%s", self._card_id, self._viewer_id)
            await interaction.response.send_message(
                "Could not update wishlist. Try again.", ephemeral=True
            )
            return

        self.emoji = self._STAR
        self.style = (
            discord.ButtonStyle.primary if self._wishlisted else discord.ButtonStyle.secondary
        )
        await interaction.response.edit_message(view=self.view)

    def update(self, *, card_id: int, wishlisted: bool) -> None:
        """Refresh the button state (e.g. after flipping to a new card)."""
        self._card_id = card_id
        self._wishlisted = wishlisted
        self.emoji = self._STAR
        self.style = (
            discord.ButtonStyle.primary if wishlisted else discord.ButtonStyle.secondary
        )
