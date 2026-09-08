"""allow multiple payments per subscription

Revision ID: a6d9274f1c03
Revises: e8f4d2a190ab
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a6d9274f1c03"
down_revision: Union[str, Sequence[str], None] = "e8f4d2a190ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_payments_subscription_id", "payments", type_="unique")
    op.create_index(
        "ix_payments_subscription_id",
        "payments",
        ["subscription_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_payments_subscription_id", table_name="payments")
    op.create_unique_constraint(
        "uq_payments_subscription_id",
        "payments",
        ["subscription_id"],
    )
