"""client access controls and connection activity

Revision ID: f7a2c21e9b44
Revises: e4c729a6b132
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7a2c21e9b44"
down_revision: Union[str, Sequence[str], None] = "e4c729a6b132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vpn_clients", sa.Column("config_override", sa.Text()))
    op.add_column("vpn_clients", sa.Column("last_connected_at", sa.DateTime(timezone=True)))
    op.add_column("vpn_clients", sa.Column("last_ip", sa.String(64)))
    op.create_index(
        "ix_vpn_clients_last_connected_at",
        "vpn_clients",
        ["last_connected_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_vpn_clients_last_connected_at", table_name="vpn_clients")
    op.drop_column("vpn_clients", "last_ip")
    op.drop_column("vpn_clients", "last_connected_at")
    op.drop_column("vpn_clients", "config_override")
