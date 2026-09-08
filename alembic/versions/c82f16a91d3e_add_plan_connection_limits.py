"""add plan connection limits

Revision ID: c82f16a91d3e
Revises: b91c7d23e640
"""

from alembic import op
import sqlalchemy as sa


revision = "c82f16a91d3e"
down_revision = "b91c7d23e640"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("max_connections", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "vpn_clients",
        sa.Column("max_connections", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("vpn_clients", "max_connections")
    op.drop_column("plans", "max_connections")
