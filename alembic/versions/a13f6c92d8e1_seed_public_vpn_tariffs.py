"""seed the three public VPN tariffs

Revision ID: a13f6c92d8e1
Revises: f7a2c21e9b44
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a13f6c92d8e1"
down_revision: Union[str, Sequence[str], None] = "f7a2c21e9b44"
branch_labels = None
depends_on = None


plans = sa.table(
    "plans",
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("duration_days", sa.Integer),
    sa.column("price", sa.Numeric),
    sa.column("currency", sa.String),
    sa.column("is_active", sa.Boolean),
    sa.column("is_public", sa.Boolean),
)


CATALOG = (
    ("vpn_14d", "2 недели", 14, 200),
    ("vpn_30d", "1 месяц", 30, 300),
    ("vpn_90d", "3 месяца", 90, 600),
)


def upgrade() -> None:
    connection = op.get_bind()
    for code, name, days, price in CATALOG:
        exists = connection.execute(
            sa.select(plans.c.code).where(plans.c.code == code)
        ).scalar_one_or_none()
        values = {
            "name": name,
            "duration_days": days,
            "price": price,
            "currency": "RUB",
            "is_active": True,
            "is_public": True,
        }
        if exists is None:
            connection.execute(sa.insert(plans).values(code=code, **values))
        else:
            connection.execute(
                sa.update(plans).where(plans.c.code == code).values(**values)
            )


def downgrade() -> None:
    op.get_bind().execute(
        sa.update(plans)
        .where(plans.c.code.in_([item[0] for item in CATALOG]))
        .values(is_active=False, is_public=False)
    )
