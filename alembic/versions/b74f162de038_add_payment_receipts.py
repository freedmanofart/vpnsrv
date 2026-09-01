"""store uploaded payment receipts

Revision ID: b74f162de038
Revises: a63de15b29c4
"""

from alembic import op
import sqlalchemy as sa


revision = "b74f162de038"
down_revision = "a63de15b29c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("receipt_data", sa.LargeBinary(), nullable=True))
    op.add_column("payments", sa.Column("receipt_mime_type", sa.String(128), nullable=True))
    op.add_column("payments", sa.Column("receipt_filename", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("payments", "receipt_filename")
    op.drop_column("payments", "receipt_mime_type")
    op.drop_column("payments", "receipt_data")
