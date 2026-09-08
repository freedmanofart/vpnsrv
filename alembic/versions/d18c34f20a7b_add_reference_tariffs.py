"""add public Lite, Standard and Ultra tariffs

Revision ID: d18c34f20a7b
Revises: c82f16a91d3e
"""

from alembic import op
import sqlalchemy as sa


revision = "d18c34f20a7b"
down_revision = "c82f16a91d3e"
branch_labels = None
depends_on = None


PLANS = (
    ("lite_1d", "1 день", 1, 5, 250, "70.00"),
    ("lite_1m", "1 мес (-3%)", 30, 5, 250, "490.00"),
    ("lite_3m", "3 мес (-13%)", 90, 5, 250, "1329.00"),
    ("lite_6m", "6 мес (-21%)", 180, 5, 250, "2399.00"),
    ("lite_1y", "1 год (-31%)", 365, 5, 250, "4199.00"),
    ("lite_2y", "2 года (-38%)", 730, 5, 250, "7499.00"),
    ("standard_1m", "1 мес (-16%)", 30, 15, 650, "699.00"),
    ("standard_3m", "3 мес (-25%)", 90, 15, 650, "1869.00"),
    ("standard_6m", "6 мес (-34%)", 180, 15, 650, "3279.00"),
    ("standard_1y", "1 год (-43%)", 365, 15, 650, "5699.00"),
    ("standard_2y", "2 года (-60%)", 730, 15, 650, "7999.00"),
    ("ultra_1m", "1 мес (-23%)", 30, 30, 3072, "1199.00"),
    ("ultra_3m", "3 мес (-34%)", 90, 30, 3072, "3069.00"),
    ("ultra_6m", "6 мес (-45%)", 180, 30, 3072, "5099.00"),
    ("ultra_1y", "1 год (-56%)", 365, 30, 3072, "8099.00"),
    ("ultra_2y", "2 года (-67%)", 730, 30, 3072, "11999.00"),
)


def upgrade() -> None:
    op.add_column("plans", sa.Column("traffic_limit_gb", sa.Integer(), server_default="0", nullable=False))
    op.add_column("vpn_clients", sa.Column("traffic_limit_gb", sa.Integer(), server_default="0", nullable=False))
    codes = ", ".join(f"'{code}'" for code, *_ in PLANS)
    op.execute(f"UPDATE plans SET is_public = false WHERE code NOT IN ({codes})")
    op.execute(
        f"""
        DELETE FROM plans
        WHERE code NOT IN ({codes})
          AND NOT EXISTS (SELECT 1 FROM subscriptions WHERE subscriptions.plan_id = plans.id)
          AND NOT EXISTS (SELECT 1 FROM payments WHERE payments.plan_id = plans.id)
        """
    )
    for code, name, days, connections, traffic, price in PLANS:
        op.execute(
            """
            INSERT INTO plans
                (code, name, duration_days, max_connections, traffic_limit_gb, price, currency,
                 is_active, is_public)
            VALUES
                ('%s', '%s', %d, %d, %d, %s, 'RUB', true, true)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                duration_days = EXCLUDED.duration_days,
                max_connections = EXCLUDED.max_connections,
                traffic_limit_gb = EXCLUDED.traffic_limit_gb,
                price = EXCLUDED.price,
                currency = EXCLUDED.currency,
                is_active = true,
                is_public = true
            """
            % (code, name, days, connections, traffic, price)
        )


def downgrade() -> None:
    codes = ", ".join(f"'{code}'" for code, *_ in PLANS)
    op.execute(f"UPDATE plans SET is_active = false, is_public = false WHERE code IN ({codes})")
    op.drop_column("vpn_clients", "traffic_limit_gb")
    op.drop_column("plans", "traffic_limit_gb")
