"""reduce Standard and Ultra plan prices

Revision ID: d91f4a6c2b70
Revises: c8e4a1f093d2
"""

from decimal import Decimal

from alembic import op
import sqlalchemy as sa


revision = "d91f4a6c2b70"
down_revision = "c8e4a1f093d2"
branch_labels = None
depends_on = None


# Prices are approximately 30% below the previous values and rounded to clean,
# customer-facing amounts. Hidden plans are included so re-enabling one later
# does not restore the old price level.
PRICE_CHANGES = (
    ("standard_1m", Decimal("699.00"), Decimal("490.00")),
    ("standard_3m", Decimal("1869.00"), Decimal("1290.00")),
    ("standard_6m", Decimal("3279.00"), Decimal("2290.00")),
    ("standard_1y", Decimal("5699.00"), Decimal("3990.00")),
    ("standard_2y", Decimal("7999.00"), Decimal("5590.00")),
    ("ultra_1m", Decimal("1199.00"), Decimal("840.00")),
    ("ultra_3m", Decimal("3069.00"), Decimal("2150.00")),
    ("ultra_6m", Decimal("5099.00"), Decimal("3590.00")),
    ("ultra_1y", Decimal("8099.00"), Decimal("5690.00")),
    ("ultra_2y", Decimal("11999.00"), Decimal("8390.00")),
)


def _set_prices(*, use_new_prices: bool) -> None:
    statement = sa.text(
        "UPDATE plans SET price = :price WHERE code = :code"
    )
    for code, old_price, new_price in PRICE_CHANGES:
        op.execute(
            statement.bindparams(
                code=code,
                price=new_price if use_new_prices else old_price,
            )
        )


def upgrade() -> None:
    _set_prices(use_new_prices=True)


def downgrade() -> None:
    _set_prices(use_new_prices=False)
