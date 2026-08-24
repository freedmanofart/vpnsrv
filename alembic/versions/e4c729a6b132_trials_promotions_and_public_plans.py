"""trials, promotions and public plans

Revision ID: e4c729a6b132
Revises: d8e26f19a4c1
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4c729a6b132"
down_revision: Union[str, Sequence[str], None] = "d8e26f19a4c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("is_public", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_table(
        "access_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "kind", "code", name="uq_access_grant_user_kind_code"
        ),
    )
    op.create_index("ix_access_grants_user_id", "access_grants", ["user_id"])


def downgrade() -> None:
    op.drop_table("access_grants")
    op.drop_column("plans", "is_public")
