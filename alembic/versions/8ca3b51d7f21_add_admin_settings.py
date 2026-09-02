"""add admin settings

Revision ID: 8ca3b51d7f21
Revises: 7b9e2d4a1c03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "8ca3b51d7f21"
down_revision: Union[str, Sequence[str], None] = "7b9e2d4a1c03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.String(length=1000), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

def downgrade() -> None:
    op.drop_table("admin_settings")
