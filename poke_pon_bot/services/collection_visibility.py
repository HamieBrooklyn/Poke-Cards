"""Which owned copies count as “in your collection” for browsing / selling / evolving."""

from __future__ import annotations

from sqlalchemy import exists, select

from poke_pon_bot.models.auction import AUCTION_STATUS_ACTIVE, CardAuction
from poke_pon_bot.models.inventory import UserCardInstance


def user_instance_not_in_active_auction():
    """SQL predicate: this ``UserCardInstance`` has no **active** auction listing.

    While a copy is listed, it stays in the seller’s inventory for settlement logic, but
    should not appear in collection UIs or be sold/evolved until the listing ends.
    """
    return ~exists(
        select(1)
        .select_from(CardAuction)
        .where(
            CardAuction.instance_id == UserCardInstance.id,
            CardAuction.status == AUCTION_STATUS_ACTIVE,
        )
    )
