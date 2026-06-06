"""Classify catalog printings for crafting (item vs craft trainer)."""



from __future__ import annotations



import json

import re



from poke_pon_bot.models.card import Card

from poke_pon_bot.models.inventory import UserCardInstance



CRAFT_ITEM_COUNT = 5

CRAFT_TRAINER_MAX_USES = 3



_CRAFT_TRAINER_SUBTYPES = ("Supporter", "Stadium", "Tool")



_ITEM_NAME_HINTS = (

    "Potion",

    "Ball",

    "Berry",

    "Mail",

    "Rod",

    "Stone",

    "Pass",

    "Candy",

    "Module",

    "Toolkit",

    "Glove",

    "Charm",

    "Case",

    "Box",

    "Capsule",

    "Ticket",

    "Disc",

    "Energy",

    "Tin",

    "Fossil",

    "Amber",

    "Incense",

    "Repel",

    "Vest",

    "Coat",

    "Scroll",

)





def card_subtypes(card: Card) -> list[str]:

    """Normalize ``tcg_subtypes`` from DB (list, JSON string, or single value)."""

    raw = getattr(card, "tcg_subtypes", None)

    if raw is None:

        return []

    if isinstance(raw, list):

        return [str(x).strip() for x in raw if str(x).strip()]

    if isinstance(raw, str):

        s = raw.strip()

        if not s:

            return []

        if s.startswith("["):

            try:

                parsed = json.loads(s)

            except json.JSONDecodeError:

                return [s]

            if isinstance(parsed, list):

                return [str(x).strip() for x in parsed if str(x).strip()]

        return [s]

    return []





def _supertype(card: Card) -> str:

    return (card.supertype or "").strip()





def _has_subtype(card: Card, label: str) -> bool:

    want = label.casefold()

    return any(s.casefold() == want for s in card_subtypes(card))





def _name_looks_like_item(name: str) -> bool:

    """Fallback when tcg_subtypes missing — whole-word match to avoid false positives."""

    n = (name or "").strip()

    if not n:

        return False

    for hint in _ITEM_NAME_HINTS:

        if re.search(r"\b" + re.escape(hint) + r"\b", n, re.IGNORECASE):

            return True

    return False





def is_item_card(card: Card) -> bool:

    """Item trainer or Energy printing used as crafting material (5 required)."""

    st = _supertype(card)

    if st == "Energy":

        return True

    if st != "Trainer":

        return False

    if _has_subtype(card, "Item"):

        return True

    if _has_subtype(card, "Pokémon Tool"):

        return True

    for label in _CRAFT_TRAINER_SUBTYPES:

        if _has_subtype(card, label):

            return False

    if card_subtypes(card):

        return False

    return _name_looks_like_item(card.name or "")





def is_craft_trainer_card(card: Card) -> bool:

    """Supporter / Stadium / Tool trainer used to set crafted pack tier (3 uses)."""

    if _supertype(card) != "Trainer":

        return False

    if _has_subtype(card, "Item"):

        return False

    return any(_has_subtype(card, label) for label in _CRAFT_TRAINER_SUBTYPES)





def initial_craft_uses_for_card(card: Card) -> int | None:

    if is_craft_trainer_card(card):

        return CRAFT_TRAINER_MAX_USES

    return None





def apply_new_instance_craft_uses(inst: UserCardInstance, card: Card) -> None:

    inst.craft_uses_remaining = initial_craft_uses_for_card(card)





def craft_uses_payload(inst: UserCardInstance, card: Card) -> dict | None:

    """API / web: dots for craft trainers only."""

    if not is_craft_trainer_card(card):

        return None

    remaining = inst.craft_uses_remaining

    if remaining is None:

        remaining = CRAFT_TRAINER_MAX_USES

    remaining = max(0, min(CRAFT_TRAINER_MAX_USES, int(remaining)))

    return {

        "max": CRAFT_TRAINER_MAX_USES,

        "remaining": remaining,

        "used": CRAFT_TRAINER_MAX_USES - remaining,

    }





def craft_role_for_card(card: Card) -> str:

    if is_item_card(card):

        return "item"

    if is_craft_trainer_card(card):

        return "craft_trainer"

    st = _supertype(card)

    if st == "Pokémon":

        return "pokemon"

    return "other"





def format_craft_uses_discord(inst: UserCardInstance, card: Card) -> str | None:

    payload = craft_uses_payload(inst, card)

    if payload is None:

        return None

    rem = int(payload["remaining"])

    max_u = int(payload["max"])

    purple = "🟣" * rem

    grey = "⚫" * (max_u - rem)

    return purple + grey


