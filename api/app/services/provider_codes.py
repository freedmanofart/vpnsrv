import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tokens import token_hash
from app.db.models.audit import ActivationCode
from app.db.models.user import User
from app.services.audit import write_audit
from app.services.notifications import notify_provider_activation_code


DEVICE_NAME_ALPHABET = string.ascii_lowercase + string.digits


@dataclass(frozen=True)
class IssuedProviderCode:
    activation_id: int
    code: str
    device_name: str
    expires_at: datetime
    telegram_sent: bool
    email_sent: bool


def generate_device_name() -> str:
    suffix = "".join(secrets.choice(DEVICE_NAME_ALPHABET) for _ in range(6))
    return f"dev-{suffix}"


async def issue_provider_code(
    db: AsyncSession,
    user: User,
    *,
    ttl_minutes: int,
    actor_type: str,
    actor_id: str | None = None,
) -> IssuedProviderCode:
    now = datetime.now(timezone.utc)
    await db.execute(
        update(ActivationCode)
        .where(
            ActivationCode.user_id == user.id,
            ActivationCode.used_at.is_(None),
            ActivationCode.expires_at > now,
        )
        .values(expires_at=now)
    )
    code = f"{secrets.randbelow(100_000_000):08d}"
    device_name = generate_device_name()
    expires_at = now + timedelta(minutes=ttl_minutes)
    activation = ActivationCode(
        user_id=user.id,
        code_hash=token_hash(code),
        code_prefix=code[:2],
        expires_at=expires_at,
    )
    db.add(activation)
    await db.commit()
    await db.refresh(activation)

    delivery = await notify_provider_activation_code(
        db,
        user,
        code,
        device_name=device_name,
        ttl_minutes=ttl_minutes,
    )
    await write_audit(
        db,
        action="device.activation_code.create",
        result="success",
        actor_type=actor_type,
        actor_id=actor_id,
        resource_type="user",
        resource_id=user.id,
        details={
            "activation_id": activation.id,
            "expires_at": expires_at.isoformat(),
            "telegram_sent": delivery.telegram_sent,
            "email_sent": delivery.email_sent,
        },
    )
    return IssuedProviderCode(
        activation_id=activation.id,
        code=code,
        device_name=device_name,
        expires_at=expires_at,
        telegram_sent=delivery.telegram_sent,
        email_sent=delivery.email_sent,
    )
