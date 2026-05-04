"""ORM models — import side effects register tables on Base.metadata."""

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.combat_deck import UserCombatDeck
from poke_pon_bot.models.drops import DropTable, DropWeight
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.models.pending_trade import PendingTrade
from poke_pon_bot.models.pokedollars import UserPokedollars
from poke_pon_bot.models.rarity import RarityClass, TcgRarityMapping

__all__ = [
    "Card",
    "UserCombatDeck",
    "DropTable",
    "DropWeight",
    "RarityClass",
    "TcgRarityMapping",
    "UserCardInstance",
    "PendingTrade",
    "UserPokedollars",
]
