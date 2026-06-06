"""Fix Ancient Mew images (Bulbapedia 403) and rarity tiers."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "045_fix_ancient_mew"
down_revision: Union[str, None] = "044_ancient_mew"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Served from poke_pon_bot/static/manual-cards/ via the bot web app.
_STATIC = "/static/manual-cards/ancient-mew"


def upgrade() -> None:
    op.execute(
        sa.text(f"""
        UPDATE cards SET
            rarity_class_id = CASE tcg_card_id
                WHEN 'promo-ancientmew-2000-international' THEN 2
                WHEN 'promo-ancientmew-2019-jp-reprint' THEN 3
                WHEN 'promo-ancientmew-2020-korean' THEN 3
                WHEN 'promo-ancientmew-1999-jp-cosmos' THEN 4
                WHEN 'promo-ancientmew-1999-jp-speckle-nintedo' THEN 5
                WHEN 'promo-ancientmew-1999-jp-speckle-corrected' THEN 5
                ELSE rarity_class_id
            END,
            image_small_url = CASE tcg_card_id
                WHEN 'promo-ancientmew-2000-international' THEN '{_STATIC}/cosmos-small.jpg'
                WHEN 'promo-ancientmew-1999-jp-cosmos' THEN '{_STATIC}/cosmos-small.jpg'
                WHEN 'promo-ancientmew-1999-jp-speckle-nintedo' THEN '{_STATIC}/speckle-small.jpg'
                WHEN 'promo-ancientmew-1999-jp-speckle-corrected' THEN '{_STATIC}/speckle-small.jpg'
                WHEN 'promo-ancientmew-2019-jp-reprint' THEN '{_STATIC}/speckle-small.jpg'
                WHEN 'promo-ancientmew-2020-korean' THEN '{_STATIC}/speckle-small.jpg'
                ELSE image_small_url
            END,
            image_large_url = CASE tcg_card_id
                WHEN 'promo-ancientmew-2000-international' THEN '{_STATIC}/cosmos-large.jpg'
                WHEN 'promo-ancientmew-1999-jp-cosmos' THEN '{_STATIC}/cosmos-large.jpg'
                WHEN 'promo-ancientmew-1999-jp-speckle-nintedo' THEN '{_STATIC}/speckle-large.jpg'
                WHEN 'promo-ancientmew-1999-jp-speckle-corrected' THEN '{_STATIC}/speckle-large.jpg'
                WHEN 'promo-ancientmew-2019-jp-reprint' THEN '{_STATIC}/speckle-large.jpg'
                WHEN 'promo-ancientmew-2020-korean' THEN '{_STATIC}/speckle-large.jpg'
                ELSE image_large_url
            END
        WHERE tcg_card_id LIKE 'promo-ancientmew-%'
        """)
    )


def downgrade() -> None:
    op.execute(
        sa.text("""
        UPDATE cards SET
            rarity_class_id = 10,
            image_small_url = 'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg',
            image_large_url = 'https://archives.bulbagarden.net/media/upload/6/63/AncientMewPromo.jpg'
        WHERE tcg_card_id LIKE 'promo-ancientmew-%'
        """)
    )
