"""merge_user_wishlists_and_assembly

Revision ID: 92dc08eefb86
Revises: 018_user_wishlists, 043_card_assembly
Create Date: 2026-05-24 20:49:46.409757

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '92dc08eefb86'
down_revision: Union[str, None] = ('018_user_wishlists', '043_card_assembly')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
