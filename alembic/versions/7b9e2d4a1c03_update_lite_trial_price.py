"""update lite one day tariff price

Revision ID: 7b9e2d4a1c03
Revises: c91a08f4d211
"""

from alembic import op


revision = "7b9e2d4a1c03"
down_revision = "c91a08f4d211"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE plans SET price = 70.00 WHERE code = 'lite_1d'")


def downgrade() -> None:
    op.execute("UPDATE plans SET price = 17.00 WHERE code = 'lite_1d'")
