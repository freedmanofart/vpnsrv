"""operations, node agent and device foundation

Revision ID: d8e26f19a4c1
Revises: c3f8a91e74bd
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e26f19a4c1"
down_revision: Union[str, Sequence[str], None] = "c3f8a91e74bd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vpn_nodes",
        sa.Column("health_status", sa.String(32), server_default="unknown", nullable=False),
    )
    op.add_column("vpn_nodes", sa.Column("last_seen_at", sa.DateTime(timezone=True)))
    op.add_column("vpn_nodes", sa.Column("latency_ms", sa.Float()))
    op.add_column(
        "vpn_nodes",
        sa.Column("active_connections", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index("ix_vpn_nodes_health_status", "vpn_nodes", ["health_status"])

    op.create_table(
        "debug_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_debug_sessions_status", "debug_sessions", ["status"])
    op.create_index("ix_debug_sessions_expires_at", "debug_sessions", ["expires_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.String(64)),
        sa.Column("actor_type", sa.String(32), server_default="system", nullable=False),
        sa.Column("actor_id", sa.String(255)),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.String(128)),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("node_id", sa.Integer(), sa.ForeignKey("vpn_nodes.id")),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("details", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("sensitive", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"])
    op.create_index("ix_audit_logs_node_id", "audit_logs", ["node_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_action_created", "audit_logs", ["action", "created_at"])
    op.create_index("ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"])

    op.create_table(
        "node_agent_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("node_id", sa.Integer(), sa.ForeignKey("vpn_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("node_id", name="uq_node_agent_credentials_node_id"),
        sa.UniqueConstraint("token_hash", name="uq_node_agent_credentials_token_hash"),
    )
    op.create_index("ix_node_agent_credentials_node_id", "node_agent_credentials", ["node_id"], unique=True)

    op.create_table(
        "client_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("token_hash", name="uq_client_devices_token_hash"),
    )
    op.create_index("ix_client_devices_user_id", "client_devices", ["user_id"])
    op.create_index("ix_client_devices_status", "client_devices", ["status"])

    op.create_table(
        "activation_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("code_prefix", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("client_devices.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("code_hash", name="uq_activation_codes_code_hash"),
    )
    op.create_index("ix_activation_codes_user_id", "activation_codes", ["user_id"])
    op.create_index("ix_activation_codes_expires_at", "activation_codes", ["expires_at"])


def downgrade() -> None:
    op.drop_table("activation_codes")
    op.drop_table("client_devices")
    op.drop_table("node_agent_credentials")
    op.drop_table("audit_logs")
    op.drop_table("debug_sessions")
    op.drop_index("ix_vpn_nodes_health_status", table_name="vpn_nodes")
    op.drop_column("vpn_nodes", "active_connections")
    op.drop_column("vpn_nodes", "latency_ms")
    op.drop_column("vpn_nodes", "last_seen_at")
    op.drop_column("vpn_nodes", "health_status")
