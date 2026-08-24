import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import require_api_access
from app.db.models.payment import Payment
from app.db.session import get_db
from app.schemas.payment import PaymentCreate, PaymentResponse, PaymentWebhook
from app.services.payments import (
    PaymentError,
    PaymentInvalidTransition,
    PaymentNotFound,
    PaymentProvisioningError,
    create_payment,
    process_payment_event,
)


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
        return payment
    except PaymentError as exc:
        raise _payment_error(exc) from exc


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
