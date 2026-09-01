"""add payment method QR images

Revision ID: a63de15b29c4
Revises: f51ac870d296
"""

from alembic import op
import sqlalchemy as sa


revision = "a63de15b29c4"
down_revision = "f51ac870d296"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_methods", sa.Column("image_data", sa.LargeBinary(), nullable=True))
    op.add_column("payment_methods", sa.Column("image_mime_type", sa.String(64), nullable=True))
    op.add_column("payment_methods", sa.Column("image_filename", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("payment_methods", "image_filename")
    op.drop_column("payment_methods", "image_mime_type")
    op.drop_column("payment_methods", "image_data")
