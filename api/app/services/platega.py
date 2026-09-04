from __future__ import annotations

from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.payment import Payment
from app.db.models.plan import Plan
from app.db.models.user import User
from app.db.models.vpn_node import VPNNode
from app.schemas.payment import PaymentCreate
from app.services.payments import PaymentInvalidTransition, PaymentNotFound


PLATEGA_METHODS = {
    "platega_sbp_qr": ("platega_method_sbp_qr", "СБП (QR)"),
    "platega_mir_card": ("platega_method_mir_card", "Карта МИР"),
    "platega_crypto": ("platega_method_crypto", "Криптовалюта"),
}


class PlategaError(Exception):
    pass


def is_platega_method(code: str) -> bool:
    return code in PLATEGA_METHODS


def _method_id(code: str) -> int:
    attr, label = PLATEGA_METHODS[code]
    raw = getattr(settings, attr)
    if raw in (None, ""):
        raise PlategaError(f"Для способа оплаты «{label}» не задан paymentMethod Platega")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise PlategaError(f"paymentMethod Platega для «{label}» должен быть числом") from exc


def _amount(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


async def create_platega_payment(
    db: AsyncSession,
    data: PaymentCreate,
    *,
    method_code: str,
    source: str,
    return_url: str | None = None,
    failed_url: str | None = None,
) -> Payment:
    if not settings.platega_merchant_id or not settings.platega_secret:
        raise PlategaError("Platega не настроена: заполните PLATEGA_MERCHANT_ID и PLATEGA_SECRET")
    if method_code not in PLATEGA_METHODS:
        raise PlategaError("Неизвестный способ оплаты Platega")

    existing = await db.scalar(select(Payment).where(Payment.idempotency_key == data.idempotency_key))
    if existing is not None:
        return existing

    user = await db.get(User, data.user_id)
    plan = await db.get(Plan, data.plan_id)
    node = await db.get(VPNNode, data.node_id)
    if user is None:
        raise PaymentNotFound("User not found")
    if plan is None or not plan.is_active:
        raise PaymentNotFound("Plan not found")
    if node is None:
        raise PaymentNotFound("VPN node not found")
    if node.status != "active":
        raise PaymentInvalidTransition("VPN node is not active")

    return_url = return_url or settings.platega_return_url or f"{settings.public_base_url.rstrip('/')}/cabinet?payment=success"
    failed_url = failed_url or settings.platega_failed_url or f"{settings.public_base_url.rstrip('/')}/cabinet?payment=failed"
    payment_method = _method_id(method_code)
    payload = {
        "paymentMethod": payment_method,
        "paymentDetails": {
            "amount": _amount(plan.price),
            "currency": plan.currency,
        },
        "description": f"Freedom VPN: {plan.name}",
        "return": return_url,
        "failedUrl": failed_url,
        "payload": data.idempotency_key,
        "metadata": {
            "userId": str(user.id),
            "userName": user.username or f"user-{user.id}",
        },
    }
    headers = {
        "X-MerchantId": settings.platega_merchant_id,
        "X-Secret": settings.platega_secret,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(base_url=settings.platega_base_url.rstrip("/"), timeout=30.0) as client:
        response = await client.post("/transaction/process", json=payload, headers=headers)
    try:
        response_data = response.json()
    except ValueError as exc:
        raise PlategaError(f"Platega вернула не JSON: HTTP {response.status_code}") from exc
    if response.status_code >= 400:
        raise PlategaError(f"Platega отклонила платеж: HTTP {response.status_code}, {response_data}")

    provider_payment_id = response_data.get("transactionId") or response_data.get("id")
    if not provider_payment_id:
        raise PlategaError("Platega не вернула transactionId")

    payment = Payment(
        user_id=data.user_id,
        plan_id=data.plan_id,
        node_id=data.node_id,
        provider="platega",
        provider_payment_id=str(provider_payment_id),
        idempotency_key=data.idempotency_key,
        amount=plan.price,
        currency=plan.currency,
        status=str(response_data.get("status") or "PENDING").lower(),
        client_type=data.client_type,
        flow=data.flow,
        fingerprint=data.fingerprint,
        details={
            "method_code": method_code,
            "source": source,
            "platega": {
                "paymentMethod": response_data.get("paymentMethod"),
                "redirect": response_data.get("redirect") or response_data.get("url"),
                "expiresIn": response_data.get("expiresIn"),
                "return": response_data.get("return"),
                "merchantId": response_data.get("merchantId"),
                "usdtRate": response_data.get("usdtRate"),
            },
        },
    )
    db.add(payment)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        payment = await db.scalar(select(Payment).where(Payment.idempotency_key == data.idempotency_key))
        if payment is None:
            raise
    else:
        await db.refresh(payment)
    return payment
