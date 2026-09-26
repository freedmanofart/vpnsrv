"""normalize already issued mobile trials to one day and 3 GiB

Revision ID: a12c3d4e5f67
Revises: f98d4a21c7e0
"""

from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a12c3d4e5f67"
down_revision: Union[str, Sequence[str], None] = "f98d4a21c7e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TRIAL_DAYS = 1
TRIAL_TRAFFIC_BYTES = 3 * 1024**3
TRIAL_TRAFFIC_GB = 3


def _aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def upgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(timezone.utc)

    plan_rows = connection.execute(
        sa.text("SELECT id FROM plans WHERE code LIKE :prefix"),
        {"prefix": "mobile-trial-%"},
    ).fetchall()
    plan_ids = [row[0] for row in plan_rows]
    if not plan_ids:
        return

    connection.execute(
        sa.text(
            "UPDATE plans "
            "SET duration_days = :days, traffic_limit_gb = :traffic_gb, "
            "name = :name "
            "WHERE code LIKE :prefix"
        ),
        {
            "days": TRIAL_DAYS,
            "traffic_gb": TRIAL_TRAFFIC_GB,
            "name": "Тестовый доступ на 1 дн.",
            "prefix": "mobile-trial-%",
        },
    )

    subscriptions = connection.execute(
        sa.text(
            "SELECT id, user_id, starts_at, expires_at, status "
            "FROM subscriptions WHERE plan_id IN :plan_ids"
        ).bindparams(sa.bindparam("plan_ids", expanding=True)),
        {"plan_ids": plan_ids},
    ).mappings().all()

    for subscription in subscriptions:
        original_expiry = _aware(subscription["expires_at"])
        target_expiry = min(
            original_expiry,
            _aware(subscription["starts_at"]) + timedelta(days=TRIAL_DAYS),
        )
        status = subscription["status"]
        if status == "active" and target_expiry <= now:
            status = "expired"

        connection.execute(
            sa.text(
                "UPDATE subscriptions SET expires_at = :expires_at, "
                "status = :status, traffic_limit_bytes = :traffic "
                "WHERE id = :id"
            ),
            {
                "id": subscription["id"],
                "expires_at": target_expiry,
                "status": status,
                "traffic": TRIAL_TRAFFIC_BYTES,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE vpn_clients SET expires_at = CASE "
                "WHEN expires_at > :expires_at THEN :expires_at ELSE expires_at END, "
                "traffic_limit_gb = :traffic_gb "
                "WHERE subscription_id = :subscription_id"
            ),
            {
                "expires_at": target_expiry,
                "traffic_gb": TRIAL_TRAFFIC_GB,
                "subscription_id": subscription["id"],
            },
        )
        connection.execute(
            sa.text(
                "UPDATE client_devices SET expires_at = CASE "
                "WHEN expires_at > :expires_at THEN :expires_at ELSE expires_at END "
                "WHERE user_id = :user_id"
            ),
            {"expires_at": target_expiry, "user_id": subscription["user_id"]},
        )


def downgrade() -> None:
    # The previous duration/quota are deployment-specific and cannot be
    # reconstructed safely. Restore only the plan label convention.
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE plans SET name = 'Мобильный тестовый доступ' "
            "WHERE code LIKE :prefix"
        ),
        {"prefix": "mobile-trial-%"},
    )
