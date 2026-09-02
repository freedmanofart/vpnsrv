import hashlib
import hmac
import json
import base64
import binascii
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import require_api_access
from app.db.models.payment import Payment
from app.db.session import get_db
from app.schemas.payment import (
    ManualPaymentCreate,
    PaymentCreate,
    PaymentReceiptCreate,
    PaymentResponse,
    PaymentStatusUpdate,
    PaymentWebhook,
    TelegramStarsPaid,
    TelegramStarsPaymentCreate,
)
from app.services.payments import (
    PaymentError,
    PaymentInvalidTransition,
    PaymentNotFound,
    PaymentProvisioningError,
    create_payment,
    process_payment_event,
)
from app.services.notifications import notify_payment_created, notify_payment_receipt


router = APIRouter(prefix="/payments", tags=["Payments"])


def _payment_error(exc: PaymentError) -> HTTPException:
    if isinstance(exc, PaymentNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PaymentInvalidTransition):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PaymentProvisioningError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post(
    "",
    response_model=PaymentResponse,
    dependencies=[Depends(require_api_access)],
)
async def start_payment(
    data: PaymentCreate,
    db: AsyncSession = Depends(get_db),
):
    try:
        payment = await create_payment(db, data, provider=settings.payment_provider)
        if settings.payment_auto_confirm and payment.status == "pending":
            payment = await process_payment_event(
                db,
                provider=payment.provider,
                event_id=f"mock-auto-{payment.id}",
                provider_payment_id=payment.provider_payment_id or "",
                target_status="paid",
                payload={"details": {"mode": "mock-auto-confirm"}},
            )
        await notify_payment_created(db, payment)
        return payment
    except PaymentError as exc:
        raise _payment_error(exc) from exc


@router.post(
    "/manual",
    response_model=PaymentResponse,
    dependencies=[Depends(require_api_access)],
)
async def start_manual_payment(data: ManualPaymentCreate, db: AsyncSession = Depends(get_db)):
    try:
        payment = await create_payment(
            db,
            PaymentCreate(**data.model_dump(exclude={"method_code"})),
            provider="manual_bank",
        )
        payment.details = {**(payment.details or {}), "method_code": data.method_code}
        await db.commit()
        await db.refresh(payment)
        await notify_payment_created(db, payment)
        return payment
    except PaymentError as exc:
        raise _payment_error(exc) from exc


@router.post(
    "/telegram-stars",
    response_model=PaymentResponse,
    dependencies=[Depends(require_api_access)],
)
async def start_telegram_stars_payment(
    data: TelegramStarsPaymentCreate,
    db: AsyncSession = Depends(get_db),
):
    try:
        payment = await create_payment(
            db,
            PaymentCreate(**data.model_dump(exclude={"stars_amount"})),
            provider="telegram_stars",
        )
        payment.currency = "XTR"
        payment.amount = data.stars_amount
        payment.details = {
            **(payment.details or {}),
            "stars_amount": data.stars_amount,
            "source": "telegram_stars",
        }
        await db.commit()
        await db.refresh(payment)
        await notify_payment_created(db, payment)
        return payment
    except PaymentError as exc:
        raise _payment_error(exc) from exc


@router.post(
    "/telegram-stars/paid",
    response_model=PaymentResponse,
    dependencies=[Depends(require_api_access)],
)
async def confirm_telegram_stars_payment(
    data: TelegramStarsPaid,
    db: AsyncSession = Depends(get_db),
):
    existing = await db.scalar(
        select(Payment).where(
            Payment.provider == "telegram_stars",
            Payment.provider_payment_id == data.provider_payment_id,
        )
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    if existing.user_id != data.user_id:
        raise HTTPException(status_code=409, detail="Payment user mismatch")
    try:
        payment = await process_payment_event(
            db,
            provider="telegram_stars",
            event_id=data.telegram_payment_charge_id,
            provider_payment_id=data.provider_payment_id,
            target_status="paid",
            payload={
                "details": {
                    "telegram_payment_charge_id": data.telegram_payment_charge_id,
                    "invoice_payload": data.invoice_payload,
                    "source": "telegram_stars",
                }
            },
        )
    except PaymentError as exc:
        raise _payment_error(exc) from exc
    if payment.user_id != data.user_id:
        raise HTTPException(status_code=409, detail="Payment user mismatch")
    return payment


@router.post(
    "/{payment_id}/status",
    response_model=PaymentResponse,
    dependencies=[Depends(require_api_access)],
)
async def update_payment_status_from_service(
    payment_id: int,
    data: PaymentStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    if not payment.provider_payment_id:
        raise HTTPException(status_code=409, detail="Payment has no provider ID")
    try:
        return await process_payment_event(
            db,
            provider=payment.provider,
            event_id=f"service-{payment.id}-{data.status}-{uuid4()}",
            provider_payment_id=payment.provider_payment_id,
            target_status=data.status,
            payload={"details": {"source": "telegram-admin-button"}},
        )
    except PaymentError as exc:
        raise _payment_error(exc) from exc


@router.post(
    "/{payment_id}/receipt",
    response_model=PaymentResponse,
    dependencies=[Depends(require_api_access)],
)
async def attach_payment_receipt(
    payment_id: int,
    data: PaymentReceiptCreate,
    db: AsyncSession = Depends(get_db),
):
    payment = await db.get(Payment, payment_id)
    if payment is None or payment.user_id != data.user_id:
        raise HTTPException(status_code=404, detail="Payment not found")
    is_legacy_backfill = bool(
        payment.provider == "manual_bank"
        and payment.receipt_data is None
        and (payment.details or {}).get("receipt")
    )
    if payment.provider != "manual_bank" or (
        payment.status not in {"pending", "processing"} and not is_legacy_backfill
    ):
        raise HTTPException(status_code=409, detail="Payment does not accept a receipt")
    if payment.status == "pending":
        payment.status = "processing"
    try:
        receipt_data = base64.b64decode(data.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid receipt data") from exc
    if not receipt_data or len(receipt_data) > 8_000_000:
        raise HTTPException(status_code=413, detail="Receipt must be between 1 byte and 8 MB")
    payment.receipt_data = receipt_data
    payment.receipt_mime_type = data.mime_type
    payment.receipt_filename = data.filename
    payment.details = {
        **(payment.details or {}),
        "receipt": {
            "telegram_file_id": data.telegram_file_id,
            "telegram_file_unique_id": data.telegram_file_unique_id,
            "media_type": data.media_type,
        },
    }
    await db.commit()
    await db.refresh(payment)
    await notify_payment_receipt(db, payment)
    return payment


@router.get(
    "/{payment_id}",
    response_model=PaymentResponse,
    dependencies=[Depends(require_api_access)],
)
async def get_payment(payment_id: int, db: AsyncSession = Depends(get_db)):
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


@router.post("/webhooks/{provider}", response_model=PaymentResponse)
async def payment_webhook(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    event_id: str = Header(alias="X-Payment-Event-Id"),
    signature: str = Header(alias="X-Payment-Signature"),
):
    body = await request.body()
    expected = hmac.new(
        settings.payment_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        data = PaymentWebhook.model_validate(json.loads(body))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from exc
    try:
        return await process_payment_event(
            db,
            provider=provider,
            event_id=event_id,
            provider_payment_id=data.provider_payment_id,
            target_status=data.status,
            payload=data.model_dump(mode="json"),
            occurred_at=data.occurred_at,
        )
    except PaymentError as exc:
        raise _payment_error(exc) from exc
