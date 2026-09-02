"""add cabinet email login codes

Revision ID: c91a08f4d211
Revises: b74e2c31a190
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c91a08f4d211"
down_revision: Union[str, Sequence[str], None] = "b74e2c31a190"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cabinet_login_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_cabinet_login_codes_user_id", "cabinet_login_codes", ["user_id"]
    )


def downgrade() -> None:
    op.drop_table("cabinet_login_codes")
