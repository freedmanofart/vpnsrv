from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PaymentCreate(BaseModel):
    user_id: int
    plan_id: int
    node_id: int
    client_type: str = "universal"
    flow: str = ""
    fingerprint: str = "chrome"
    idempotency_key: str = Field(min_length=8, max_length=255)


class PaymentWebhook(BaseModel):
    provider_payment_id: str
    status: str
    occurred_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    plan_id: int
    node_id: int | None
    subscription_id: int | None
    provider: str
    provider_payment_id: str | None
    amount: Decimal
    currency: str
    status: str
    client_type: str
    flow: str
    fingerprint: str
    created_at: datetime
    updated_at: datetime
    paid_at: datetime | None
    failed_at: datetime | None
    cancelled_at: datetime | None
    refunded_at: datetime | None
