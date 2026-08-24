"""add vless client options

Revision ID: 9d1db660812a
Revises: 45a8774c25bc
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "9d1db660812a"
down_revision: Union[str, Sequence[str], None] = "45a8774c25bc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vpn_clients", sa.Column("client_type", sa.String(32), server_default="universal", nullable=False))
    op.add_column("vpn_clients", sa.Column("flow", sa.String(64), server_default="", nullable=False))
    op.add_column("vpn_clients", sa.Column("fingerprint", sa.String(32), server_default="chrome", nullable=False))


def downgrade() -> None:
    op.drop_column("vpn_clients", "fingerprint")
    op.drop_column("vpn_clients", "flow")
    op.drop_column("vpn_clients", "client_type")
