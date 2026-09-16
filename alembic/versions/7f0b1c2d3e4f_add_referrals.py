"""add referral relationships and rewards

Revision ID: 7f0b1c2d3e4f
Revises: f24d8b730e11, e12a7c4d9b31
"""
from alembic import op
import sqlalchemy as sa

revision = "7f0b1c2d3e4f"
down_revision = ("f24d8b730e11", "e12a7c4d9b31")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("referred_by_user_id", sa.Integer(), nullable=True))
    op.create_index("ix_users_referred_by_user_id", "users", ["referred_by_user_id"])
    op.create_foreign_key("fk_users_referred_by_user_id", "users", "users", ["referred_by_user_id"], ["id"])
    op.create_table(
        "referral_rewards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("inviter_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("invitee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("rewarded_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("invitee_id", name="uq_referral_rewards_invitee"),
        sa.UniqueConstraint("payment_id", name="uq_referral_rewards_payment_id"),
    )
    op.create_index("ix_referral_rewards_inviter_id", "referral_rewards", ["inviter_id"])
    op.create_index("ix_referral_rewards_invitee_id", "referral_rewards", ["invitee_id"])


def downgrade() -> None:
    op.drop_table("referral_rewards")
    op.drop_constraint("fk_users_referred_by_user_id", "users", type_="foreignkey")
    op.drop_index("ix_users_referred_by_user_id", table_name="users")
    op.drop_column("users", "referred_by_user_id")
