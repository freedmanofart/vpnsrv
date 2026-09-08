"""add web cabinet access

Revision ID: e8f4d2a190ab
Revises: b74f162de038
"""

from alembic import op
import sqlalchemy as sa


revision = "e8f4d2a190ab"
down_revision = "b74f162de038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(320), nullable=True))
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "cabinet_access_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cabinet_access_tokens_user_id", "cabinet_access_tokens", ["user_id"])
    op.create_index("ix_cabinet_access_tokens_token_hash", "cabinet_access_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_table("cabinet_access_tokens")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "email")
