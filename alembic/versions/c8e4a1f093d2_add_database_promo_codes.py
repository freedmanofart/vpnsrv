"""add database promo codes

Revision ID: c8e4a1f093d2
Revises: b5c7a92f31e4
"""

from alembic import op
import sqlalchemy as sa


revision = "c8e4a1f093d2"
down_revision = "b5c7a92f31e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("max_redemptions", sa.Integer(), nullable=True),
        sa.Column("max_redemptions_per_user", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promo_codes_code", "promo_codes", ["code"], unique=True)
    op.create_index("ix_promo_codes_plan_id", "promo_codes", ["plan_id"], unique=False)
    op.drop_constraint("uq_access_grant_user_kind_code", "access_grants", type_="unique")
    op.create_index(
        "ix_access_grants_user_kind_code",
        "access_grants",
        ["user_id", "kind", "code"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO promo_codes
            (code, plan_id, max_redemptions, max_redemptions_per_user, is_active)
        SELECT 'LIGHT1DAY', id, NULL, NULL, true
        FROM plans
        WHERE code = 'lite_1d'
        ON CONFLICT (code) DO UPDATE SET
            plan_id = EXCLUDED.plan_id,
            max_redemptions = NULL,
            max_redemptions_per_user = NULL,
            is_active = true
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM access_grants newer
        USING access_grants older
        WHERE newer.user_id = older.user_id
          AND newer.kind = older.kind
          AND newer.code = older.code
          AND newer.id > older.id
        """
    )
    op.drop_index("ix_access_grants_user_kind_code", table_name="access_grants")
    op.create_unique_constraint(
        "uq_access_grant_user_kind_code",
        "access_grants",
        ["user_id", "kind", "code"],
    )
    op.drop_index("ix_promo_codes_plan_id", table_name="promo_codes")
    op.drop_index("ix_promo_codes_code", table_name="promo_codes")
    op.drop_table("promo_codes")
