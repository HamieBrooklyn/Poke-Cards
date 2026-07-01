"""ORM models — import side effects register tables on Base.metadata."""

from poke_pon_bot.models.auction import CardAuction
from poke_pon_bot.models.card import Card
from poke_pon_bot.models.catalog_announcement import CatalogAnnouncement
from poke_pon_bot.models.card_series import CardSeries, CardSeriesSet
from poke_pon_bot.models.combat_deck import UserCombatDeck
from poke_pon_bot.models.crystals import UserCrystals
from poke_pon_bot.models.drops import DropTable, DropWeight
from poke_pon_bot.models.duel_session import DuelSession
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.pack_instance import UserPackInstance
from poke_pon_bot.models.pending_trade import PendingTrade
from poke_pon_bot.models.pokedollars import UserPokedollars
from poke_pon_bot.models.rarity import RarityClass, TcgRarityMapping
from poke_pon_bot.models.topgg_processed_vote import TopggProcessedVote
from poke_pon_bot.models.user_tutorial import UserTutorial
from poke_pon_bot.models.wishlist import UserWishlist

__all__ = [
    "CardAuction",
    "Card",
    "CatalogAnnouncement",
    "CardSeries",
    "CardSeriesSet",
    "UserCombatDeck",
    "UserCrystals",
    "DropTable",
    "DropWeight",
    "DuelSession",
    "RarityClass",
    "TcgRarityMapping",
    "UserCardInstance",
    "UserPackInstance",
    "PendingTrade",
    "UserPokedollars",
    "TopggProcessedVote",
    "UserTutorial",
    "UserWishlist",
]
