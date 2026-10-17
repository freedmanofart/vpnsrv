"""Add staged, owner-approved OpenClaw actions.

Revision ID: a8b2d91c6f04
Revises: a12c3d4e5f67
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8b2d91c6f04"
down_revision: Union[str, Sequence[str], None] = "a12c3d4e5f67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_pending_actions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("confirmed_by", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_pending_actions_status",
        "assistant_pending_actions",
        ["status"],
    )
    op.create_index(
        "ix_assistant_actions_status_expiry",
        "assistant_pending_actions",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_actions_status_expiry", table_name="assistant_pending_actions")
    op.drop_index("ix_assistant_pending_actions_status", table_name="assistant_pending_actions")
    op.drop_table("assistant_pending_actions")
