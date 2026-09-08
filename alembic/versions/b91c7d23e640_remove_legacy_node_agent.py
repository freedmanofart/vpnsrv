"""remove legacy node-agent credentials

Revision ID: b91c7d23e640
Revises: a13f6c92d8e1
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b91c7d23e640"
down_revision: Union[str, None] = "a13f6c92d8e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("node_agent_credentials")


def downgrade() -> None:
    op.create_table(
        "node_agent_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("node_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["node_id"], ["vpn_nodes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("node_id", name="uq_node_agent_credentials_node_id"),
    )
    op.create_index(
        "ix_node_agent_credentials_node_id",
        "node_agent_credentials",
        ["node_id"],
        unique=True,
    )
