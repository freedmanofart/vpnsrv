"""add configurable payment methods

Revision ID: e4a9b03c127d
Revises: d18c34f20a7b
"""

from alembic import op
import sqlalchemy as sa


revision = "e4a9b03c127d"
down_revision = "d18c34f20a7b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_methods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(2048), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    methods = (
        ("sbp", "СБП, QR (рубли)", 10),
        ("card_ru", "Карта (рубли)", 20),
        ("card_foreign", "Зарубежная карта", 30),
        ("crypto", "Крипта", 40),
        ("telegram_stars", "⭐ Telegram Stars", 50),
        ("payment_safety", "🔒 Почему оплата из РФ безопасна", 60),
    )
    for code, name, order in methods:
        op.execute(
            "INSERT INTO payment_methods (code, name, sort_order, is_active) "
            f"VALUES ('{code}', '{name}', {order}, true)"
        )


def downgrade() -> None:
    op.drop_table("payment_methods")
