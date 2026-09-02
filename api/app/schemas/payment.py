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
    fingerprint: str = "firefox"
    idempotency_key: str = Field(min_length=8, max_length=255)


class ManualPaymentCreate(PaymentCreate):
    method_code: str = Field(min_length=2, max_length=64)


class TelegramStarsPaymentCreate(PaymentCreate):
    stars_amount: int = Field(gt=0)


class TelegramStarsPaid(BaseModel):
    user_id: int
    provider_payment_id: str = Field(min_length=1, max_length=255)
    telegram_payment_charge_id: str = Field(min_length=1, max_length=255)
    invoice_payload: str = Field(min_length=1, max_length=128)


class PaymentReceiptCreate(BaseModel):
    user_id: int
    telegram_file_id: str = Field(min_length=5, max_length=1024)
    telegram_file_unique_id: str | None = Field(default=None, max_length=255)
    media_type: str = Field(pattern=r"^(photo|document)$")
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(max_length=128)
    data_base64: str = Field(min_length=4, max_length=12_000_000)


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
    details: dict[str, Any]
