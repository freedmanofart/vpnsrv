from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
from datetime import datetime, timezone
from hashlib import sha256

from app.core.config import settings


TELEGRAM_CABINET_LINK_TTL_SECONDS = 30 * 86400
INCY_SUBSCRIPTION_PURPOSE = "incy-subscription"


def _signature(payload: str, *, purpose: str) -> str:
    secret = settings.payment_webhook_secret or settings.service_api_token
    return hmac.new(secret.encode(), f"{purpose}:{payload}".encode(), sha256).hexdigest()


def telegram_cabinet_link_token(user_id: int) -> str:
    payload = json.dumps(
        {
            "user_id": user_id,
            "exp": int(datetime.now(timezone.utc).timestamp()) + TELEGRAM_CABINET_LINK_TTL_SECONDS,
            "nonce": secrets.token_urlsafe(12),
        },
        separators=(",", ":"),
    )
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_signature(encoded, purpose='telegram-cabinet-link')}"


def verify_telegram_cabinet_link_token(token: str) -> int | None:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _signature(encoded, purpose="telegram-cabinet-link")):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        user_id = int(payload["user_id"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError, binascii.Error, json.JSONDecodeError):
        return None
    if expires_at < int(datetime.now(timezone.utc).timestamp()):
        return None
    return user_id


def incy_subscription_token(client_id: int, expires_at: datetime) -> str:
    """Create a signed, non-persistent token for an INCY subscription URL."""

    payload = {
        "client_id": int(client_id),
        "exp": int(expires_at.replace(tzinfo=expires_at.tzinfo or timezone.utc).timestamp()),
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{encoded}.{_signature(encoded, purpose=INCY_SUBSCRIPTION_PURPOSE)}"


def verify_incy_subscription_token(token: str) -> int | None:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(
        signature,
        _signature(encoded, purpose=INCY_SUBSCRIPTION_PURPOSE),
    ):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        client_id = int(payload["client_id"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError, binascii.Error, json.JSONDecodeError):
        return None
    if expires_at < int(datetime.now(timezone.utc).timestamp()):
        return None
    return client_id
