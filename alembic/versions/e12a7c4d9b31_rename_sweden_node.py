"""Rename the active Sweden node for provider profile display.

Revision ID: e12a7c4d9b31
Revises: d91f4a6c2b70
"""

from alembic import op
import sqlalchemy as sa


revision = "e12a7c4d9b31"
down_revision = "d91f4a6c2b70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    existing = connection.execute(
        sa.text("SELECT id FROM vpn_nodes WHERE name = :name LIMIT 1"),
        {"name": "sweden"},
    ).scalar_one_or_none()
    if existing is None:
        connection.execute(
            sa.text("UPDATE vpn_nodes SET name = :new_name WHERE name = :old_name"),
            {"new_name": "sweden", "old_name": "node-sw"},
        )


def downgrade() -> None:
    connection = op.get_bind()
    existing = connection.execute(
        sa.text("SELECT id FROM vpn_nodes WHERE name = :name LIMIT 1"),
        {"name": "node-sw"},
    ).scalar_one_or_none()
    if existing is None:
        connection.execute(
            sa.text("UPDATE vpn_nodes SET name = :old_name WHERE name = :new_name"),
            {"old_name": "node-sw", "new_name": "sweden"},
        )
