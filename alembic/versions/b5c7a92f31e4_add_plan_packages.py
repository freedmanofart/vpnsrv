"""add plan packages

Revision ID: b5c7a92f31e4
Revises: d7c9a2b4e6f1
"""

from alembic import op
import sqlalchemy as sa


revision = "b5c7a92f31e4"
down_revision = "d7c9a2b4e6f1"
branch_labels = None
depends_on = None


PACKAGES = (
    ("lite", "Лайт", "5 подключений · 250 ГБ трафика", 5, 250, 10),
    ("standard", "Стандарт", "15 подключений · 650 ГБ трафика", 15, 650, 20),
    ("ultra", "Ультра", "30 подключений · 3 ТБ трафика", 30, 3072, 30),
)


def upgrade() -> None:
    op.create_table(
        "plan_packages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("max_connections", sa.Integer(), server_default="0", nullable=False),
        sa.Column("traffic_limit_gb", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="100", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.add_column("plans", sa.Column("package_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_plans_package_id_plan_packages",
        "plans",
        "plan_packages",
        ["package_id"],
        ["id"],
        ondelete="SET NULL",
    )

    for code, name, description, max_connections, traffic_limit_gb, sort_order in PACKAGES:
        op.execute(
            sa.text(
                """
                INSERT INTO plan_packages
                    (code, name, description, max_connections, traffic_limit_gb, sort_order, is_active)
                VALUES
                    (:code, :name, :description, :max_connections, :traffic_limit_gb, :sort_order, true)
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    max_connections = EXCLUDED.max_connections,
                    traffic_limit_gb = EXCLUDED.traffic_limit_gb,
                    sort_order = EXCLUDED.sort_order,
                    is_active = true
                """
            ).bindparams(
                code=code,
                name=name,
                description=description,
                max_connections=max_connections,
                traffic_limit_gb=traffic_limit_gb,
                sort_order=sort_order,
            )
        )

    op.execute(
        """
        UPDATE plans
        SET package_id = plan_packages.id
        FROM plan_packages
        WHERE plan_packages.code = split_part(plans.code, '_', 1)
           OR plan_packages.max_connections = plans.max_connections
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_plans_package_id_plan_packages", "plans", type_="foreignkey")
    op.drop_column("plans", "package_id")
    op.drop_table("plan_packages")
