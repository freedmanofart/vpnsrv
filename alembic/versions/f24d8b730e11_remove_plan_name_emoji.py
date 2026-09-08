"""remove emoji from plan names

Revision ID: f24d8b730e11
Revises: a6d9274f1c03
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f24d8b730e11"
down_revision: Union[str, Sequence[str], None] = "a6d9274f1c03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    replacements = {
        "lite_6m": "6 мес (-21%)",
        "lite_2y": "2 года (-38%)",
        "standard_6m": "6 мес (-34%)",
        "standard_2y": "2 года (-60%)",
        "ultra_6m": "6 мес (-45%)",
        "ultra_2y": "2 года (-67%)",
    }
    for code, name in replacements.items():
        op.execute(
            "UPDATE plans SET name = '%s' WHERE code = '%s'" % (name, code)
        )


def downgrade() -> None:
    replacements = {
        "lite_6m": "🔥 6 мес (-21%) 🔥",
        "lite_2y": "💎 2 года (-38%) 💎",
        "standard_6m": "🔥 6 мес (-34%) 🔥",
        "standard_2y": "💎 2 года (-60%) 💎",
        "ultra_6m": "🔥 6 мес (-45%) 🔥",
        "ultra_2y": "💎 2 года (-67%) 💎",
    }
    for code, name in replacements.items():
        op.execute(
            "UPDATE plans SET name = '%s' WHERE code = '%s'" % (name, code)
        )
