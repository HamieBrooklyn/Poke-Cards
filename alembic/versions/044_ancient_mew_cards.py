"""Add Ancient Mew card variants (promo cards from Pokemon: The Movie 2000)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "044_ancient_mew"
down_revision: Union[str, None] = "92dc08eefb86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Insert Ancient Mew card variants.
    
    Ancient Mew is not available in the Pokémon TCG API, so we manually insert these
    promotional cards from the Pokemon: The Movie 2000 theatrical release.
    
    Variants:
    - Ancient Mew I (Speckle Holo, "Nintedo" error) - Japanese 1999
    - Ancient Mew I (Speckle Holo, corrected) - Japanese 1999
    - Ancient Mew II (Cosmos Holo) - Japanese 1999
    - Ancient Mew (International release, Cosmos Holo) - English 2000
    - Ancient Mew 2019 (Speckle Holo reprint) - Japanese 2019
    - Ancient Mew 2020 (Speckle Holo) - Korean 2020
    
    All variants are classified as "chase" rarity (id=10) due to their extreme
    collectibility and limited distribution.
    """
    
    # Using bulk insert for all Ancient Mew variants
    # Images use Bulbapedia's Archive URLs which are stable and widely used
    # All variants use the same card image since they're visually similar; the tcg_card_id
    # distinguishes the variants (Nintedo error, corrected, cosmos holo, international, etc.)
    
    base_image_url = "https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg"
    
    op.execute(
        sa.text("""
        INSERT INTO cards (
            tcg_card_id,
            name,
            set_code,
            set_name,
            collector_number,
            tcg_rarity,
            image_small_url,
            image_large_url,
            supertype,
            hp,
            attacks,
            dex_numbers,
            tcg_types,
            rarity_class_id,
            evolves_to_names,
            evolves_from
        ) VALUES
        (
            'promo-ancientmew-1999-jp-speckle-nintedo',
            'Ancient Mew (Nintedo Error)',
            'promo',
            'Miscellaneous Promotional Cards',
            'P1',
            'Promo',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'Pokémon',
            '30',
            '[{"name": "Psyche", "damage": "40", "text": null, "cost": ["Psychic", "Psychic"]}]',
            '[151]',
            '["Psychic"]',
            10,
            NULL,
            NULL
        ),
        (
            'promo-ancientmew-1999-jp-speckle-corrected',
            'Ancient Mew (JP Corrected)',
            'promo',
            'Miscellaneous Promotional Cards',
            'P2',
            'Promo',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'Pokémon',
            '30',
            '[{"name": "Psyche", "damage": "40", "text": null, "cost": ["Psychic", "Psychic"]}]',
            '[151]',
            '["Psychic"]',
            10,
            NULL,
            NULL
        ),
        (
            'promo-ancientmew-1999-jp-cosmos',
            'Ancient Mew (JP Cosmos)',
            'promo',
            'Miscellaneous Promotional Cards',
            'P3',
            'Promo',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'Pokémon',
            '30',
            '[{"name": "Psyche", "damage": "40", "text": null, "cost": ["Psychic", "Psychic"]}]',
            '[151]',
            '["Psychic"]',
            10,
            NULL,
            NULL
        ),
        (
            'promo-ancientmew-2000-international',
            'Ancient Mew',
            'promo',
            'Miscellaneous Promotional Cards',
            'P4',
            'Promo',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'Pokémon',
            '30',
            '[{"name": "Psyche", "damage": "40", "text": null, "cost": ["Psychic", "Psychic"]}]',
            '[151]',
            '["Psychic"]',
            10,
            NULL,
            NULL
        ),
        (
            'promo-ancientmew-2019-jp-reprint',
            'Ancient Mew (2019 Reprint)',
            'promo',
            'Miscellaneous Promotional Cards',
            'P5',
            'Promo',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'Pokémon',
            '30',
            '[{"name": "Psyche", "damage": "40", "text": null, "cost": ["Psychic", "Psychic"]}]',
            '[151]',
            '["Psychic"]',
            10,
            NULL,
            NULL
        ),
        (
            'promo-ancientmew-2020-korean',
            'Ancient Mew (2020 Korean)',
            'promo',
            'Miscellaneous Promotional Cards',
            'P6',
            'Promo',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            'Pokémon',
            '30',
            '[{"name": "Psyche", "damage": "40", "text": null, "cost": ["Psychic", "Psychic"]}]',
            '[151]',
            '["Psychic"]',
            10,
            NULL,
            NULL
        )
        """)
    )


def downgrade() -> None:
    """Remove Ancient Mew card variants."""
    op.execute(
        sa.text("""
        DELETE FROM cards WHERE tcg_card_id IN (
            'promo-ancientmew-1999-jp-speckle-nintedo',
            'promo-ancientmew-1999-jp-speckle-corrected',
            'promo-ancientmew-1999-jp-cosmos',
            'promo-ancientmew-2000-international',
            'promo-ancientmew-2019-jp-reprint',
            'promo-ancientmew-2020-korean'
        )
        """)
    )
