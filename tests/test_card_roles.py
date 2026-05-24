"""Craft role classification from tcg_subtypes."""

from poke_pon_bot.models.card import Card
from poke_pon_bot.services.card_roles import (
    card_subtypes,
    craft_role_for_card,
    is_craft_trainer_card,
    is_item_card,
)


def _card(**kwargs) -> Card:
    c = Card(
        tcg_card_id=kwargs.get("tcg_card_id", "test-1"),
        name=kwargs.get("name", "Test"),
        set_code="tst",
        set_name="Test Set",
        collector_number="1",
        image_small_url="https://example.com/s.png",
        image_large_url="https://example.com/l.png",
        rarity_class_id=1,
    )
    c.supertype = kwargs.get("supertype", "Trainer")
    c.tcg_subtypes = kwargs.get("tcg_subtypes")
    return c


def test_item_trainer_with_subtype_is_material() -> None:
    card = _card(name="Suspicious Food Tin", tcg_subtypes=["Item"])
    assert is_item_card(card)
    assert not is_craft_trainer_card(card)
    assert craft_role_for_card(card) == "item"


def test_stadium_trainer_is_craft_trainer() -> None:
    card = _card(name="Mountain Ring", tcg_subtypes=["Stadium"])
    assert not is_item_card(card)
    assert is_craft_trainer_card(card)
    assert craft_role_for_card(card) == "craft_trainer"


def test_item_subtype_blocks_craft_trainer_even_with_extra_tags() -> None:
    card = _card(name="Weird", tcg_subtypes=["Item", "Supporter"])
    assert is_item_card(card)
    assert not is_craft_trainer_card(card)


def test_subtypes_json_string_parsed() -> None:
    card = _card(tcg_subtypes='["Item"]')
    assert card_subtypes(card) == ["Item"]
    assert craft_role_for_card(card) == "item"


def test_old_amber_name_fallback_when_untagged() -> None:
    card = _card(name="Old Amber Aerodactyl", tcg_subtypes=None)
    assert is_item_card(card)
    assert craft_role_for_card(card) == "item"
