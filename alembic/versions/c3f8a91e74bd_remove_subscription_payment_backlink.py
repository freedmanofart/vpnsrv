"""remove redundant subscription payment backlink

Revision ID: c3f8a91e74bd
Revises: b6c1e7a4d920
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f8a91e74bd"
down_revision: Union[str, Sequence[str], None] = "b6c1e7a4d920"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_subscriptions_payment_id", "subscriptions", type_="unique")
    op.drop_constraint("fk_subscriptions_payment_id", "subscriptions", type_="foreignkey")
    op.drop_column("subscriptions", "payment_id")


def downgrade() -> None:
    op.add_column("subscriptions", sa.Column("payment_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_subscriptions_payment_id",
        "subscriptions",
        "payments",
        ["payment_id"],
        ["id"],
    )
    op.create_unique_constraint("uq_subscriptions_payment_id", "subscriptions", ["payment_id"])
