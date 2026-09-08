"""seed Platega payment methods

Revision ID: d7c9a2b4e6f1
Revises: 8ca3b51d7f21
"""

from alembic import op


revision = "d7c9a2b4e6f1"
down_revision = "8ca3b51d7f21"
branch_labels = None
depends_on = None


METHODS = (
    ("platega_sbp_qr", "СБП (QR)", 70),
    ("platega_mir_card", "Карта МИР", 80),
    ("platega_crypto", "Криптовалюта", 90),
)


def upgrade() -> None:
    for code, name, sort_order in METHODS:
        op.execute(
            "INSERT INTO payment_methods (code, name, url, sort_order, is_active) "
            f"VALUES ('{code}', '{name}', NULL, {sort_order}, false) "
            "ON CONFLICT (code) DO UPDATE SET "
            f"name = '{name}', "
            "url = NULL, "
            f"sort_order = {sort_order}, "
            "is_active = false"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM payment_methods "
        "WHERE code IN ('platega_sbp_qr', 'platega_mir_card', 'platega_crypto')"
    )
