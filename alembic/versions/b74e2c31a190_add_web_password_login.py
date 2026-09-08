"""add web password login

Revision ID: b74e2c31a190
Revises: f24d8b730e11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b74e2c31a190"
down_revision: Union[str, Sequence[str], None] = "f24d8b730e11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_hash")
