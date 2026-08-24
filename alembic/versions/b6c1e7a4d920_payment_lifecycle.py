"""payment lifecycle and active-client constraints

Revision ID: b6c1e7a4d920
Revises: 9d1db660812a
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6c1e7a4d920"
down_revision: Union[str, Sequence[str], None] = "9d1db660812a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE vpn_node_configs
        SET config = jsonb_set(
            config::jsonb,
            '{api_address}',
            '"172.18.0.1:10085"'::jsonb,
            true
        )
        WHERE protocol = 'vless'
          AND COALESCE(config->>'api_address', '') = ''
        """
    )
    op.add_column("payments", sa.Column("idempotency_key", sa.String(255), nullable=True))
    op.add_column("payments", sa.Column("node_id", sa.Integer(), nullable=True))
    op.add_column("payments", sa.Column("subscription_id", sa.Integer(), nullable=True))
    op.add_column(
        "payments",
        sa.Column("client_type", sa.String(32), server_default="universal", nullable=False),
    )
    op.add_column(
        "payments",
        sa.Column("flow", sa.String(64), server_default="", nullable=False),
    )
    op.add_column(
        "payments",
        sa.Column("fingerprint", sa.String(32), server_default="chrome", nullable=False),
    )
    op.add_column(
        "payments",
        sa.Column("details", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
    )
    op.add_column(
        "payments",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("payments", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_payments_node_id", "payments", "vpn_nodes", ["node_id"], ["id"])
    op.create_foreign_key(
        "fk_payments_subscription_id",
        "payments",
        "subscriptions",
        ["subscription_id"],
        ["id"],
    )
    op.create_index("ix_payments_idempotency_key", "payments", ["idempotency_key"], unique=True)
    op.create_unique_constraint(
        "uq_payments_provider_payment_id",
        "payments",
        ["provider", "provider_payment_id"],
    )
    op.create_unique_constraint("uq_payments_subscription_id", "payments", ["subscription_id"])

    op.add_column("subscriptions", sa.Column("payment_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_subscriptions_payment_id", "subscriptions", "payments", ["payment_id"], ["id"]
    )
    op.create_unique_constraint("uq_subscriptions_payment_id", "subscriptions", ["payment_id"])

    op.create_table(
        "payment_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="processed", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "event_id", name="uq_payment_events_provider_event"),
    )
    op.create_index("ix_payment_events_payment_id", "payment_events", ["payment_id"])

    op.create_index(
        "uq_vpn_clients_one_active_per_subscription",
        "vpn_clients",
        ["subscription_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_subscriptions_one_active_per_user",
        "subscriptions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_subscriptions_one_active_per_user", table_name="subscriptions")
    op.drop_index("uq_vpn_clients_one_active_per_subscription", table_name="vpn_clients")
    op.drop_index("ix_payment_events_payment_id", table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_constraint("uq_subscriptions_payment_id", "subscriptions", type_="unique")
    op.drop_constraint("fk_subscriptions_payment_id", "subscriptions", type_="foreignkey")
    op.drop_column("subscriptions", "payment_id")
    op.drop_constraint("uq_payments_subscription_id", "payments", type_="unique")
    op.drop_constraint("uq_payments_provider_payment_id", "payments", type_="unique")
    op.drop_index("ix_payments_idempotency_key", table_name="payments")
    op.drop_constraint("fk_payments_subscription_id", "payments", type_="foreignkey")
    op.drop_constraint("fk_payments_node_id", "payments", type_="foreignkey")
    for name in (
        "refunded_at",
        "cancelled_at",
        "failed_at",
        "updated_at",
        "details",
        "fingerprint",
        "flow",
        "client_type",
        "subscription_id",
        "node_id",
        "idempotency_key",
    ):
        op.drop_column("payments", name)
