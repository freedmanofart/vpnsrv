"""add mobile provider profiles, trials and traffic counters

Revision ID: f98d4a21c7e0
Revises: 7f0b1c2d3e4f
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f98d4a21c7e0"
down_revision: Union[str, Sequence[str], None] = "7f0b1c2d3e4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "client_devices",
        sa.Column("install_id_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_client_devices_install_id_hash",
        "client_devices",
        ["install_id_hash"],
        unique=True,
    )
    op.add_column(
        "subscriptions",
        sa.Column("traffic_limit_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "vpn_clients",
        sa.Column("upload_bytes", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "vpn_clients",
        sa.Column("download_bytes", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.drop_index("uq_vpn_clients_one_active_per_subscription", table_name="vpn_clients")
    op.create_index(
        "uq_vpn_clients_active_node_protocol",
        "vpn_clients",
        ["subscription_id", "node_id", "protocol"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_vpn_clients_active_node_protocol", table_name="vpn_clients")
    op.create_index(
        "uq_vpn_clients_one_active_per_subscription",
        "vpn_clients",
        ["subscription_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.drop_column("vpn_clients", "download_bytes")
    op.drop_column("vpn_clients", "upload_bytes")
    op.drop_column("subscriptions", "traffic_limit_bytes")
    op.drop_index("ix_client_devices_install_id_hash", table_name="client_devices")
    op.drop_column("client_devices", "install_id_hash")
