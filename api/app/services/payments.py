from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.payment import Payment, PaymentEvent
from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_client import VPNClient
from app.schemas.payment import PaymentCreate
from app.services.provisioning import (
    ProvisioningConflict,
    ProvisioningError,
    ProvisioningInvalid,
    ProvisioningNotFound,
    ProvisioningResult,
    ProvisioningThreeXUIError,
    commit_provisioning,
    provision_subscription,
    renew_paid_subscription,
)
from app.services.threexui import ThreeXUIClient
from app.services.payment_providers import get_payment_provider
from app.services.vpn_expiration import revoke_vpn_client


class PaymentError(Exception):
    pass


class PaymentNotFound(PaymentError):
    pass


class PaymentInvalidTransition(PaymentError):
    pass


class PaymentProvisioningError(PaymentError):
    pass


STATUS_ALIASES = {
    "succeeded": "paid",
    "success": "paid",
    "canceled": "cancelled",
}

ALLOWED_TRANSITIONS = {
    "pending": {"processing", "paid", "failed", "cancelled", "expired"},
    "processing": {"paid", "failed", "cancelled", "expired"},
    "paid": {"refunded"},
    "failed": set(),
    "cancelled": set(),
    "expired": set(),
    "refunded": set(),
}


async def _payment_for_duplicate_event(
    db: AsyncSession,
    *,
    provider: str,
    event_id: str,
) -> Payment | None:
    result = await db.execute(
        select(PaymentEvent).where(
            PaymentEvent.provider == provider,
            PaymentEvent.event_id == event_id,
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        return None
    return await db.get(Payment, event.payment_id)


async def create_payment(
    db: AsyncSession,
    data: PaymentCreate,
    *,
    provider: str,
) -> Payment:
    existing_result = await db.execute(
        select(Payment).where(Payment.idempotency_key == data.idempotency_key)
    )
    existing = existing_result.scalar_one_or_none()
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

    adapter = get_payment_provider(provider)
    provider_payment = await adapter.create(idempotency_key=data.idempotency_key)
    payment = Payment(
        user_id=data.user_id,
        plan_id=data.plan_id,
        node_id=data.node_id,
        provider=provider,
        provider_payment_id=provider_payment.provider_payment_id,
        idempotency_key=data.idempotency_key,
        amount=plan.price,
        currency=plan.currency,
        status=provider_payment.status,
        client_type=data.client_type,
        flow=data.flow,
        fingerprint=data.fingerprint,
        details=provider_payment.details,
    )
    db.add(payment)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(Payment).where(Payment.idempotency_key == data.idempotency_key)
        )
        payment = result.scalar_one()
    else:
        await db.refresh(payment)
    return payment


async def process_payment_event(
    db: AsyncSession,
    *,
    provider: str,
    event_id: str,
    provider_payment_id: str,
    target_status: str,
    payload: dict,
    occurred_at: datetime | None = None,
    panel_factory: Callable[..., ThreeXUIClient] = ThreeXUIClient,
) -> Payment:
    target_status = STATUS_ALIASES.get(target_status.lower(), target_status.lower())
    if target_status not in ALLOWED_TRANSITIONS:
        raise PaymentInvalidTransition(f"Unsupported payment status: {target_status}")

    duplicate_result = await db.execute(
        select(PaymentEvent).where(
            PaymentEvent.provider == provider,
            PaymentEvent.event_id == event_id,
        )
    )
    duplicate = duplicate_result.scalar_one_or_none()
    if duplicate is not None:
        payment = await db.get(Payment, duplicate.payment_id)
        if payment is None:
            raise PaymentNotFound("Payment not found")
        return payment

    payment_result = await db.execute(
        select(Payment)
        .where(
            Payment.provider == provider,
            Payment.provider_payment_id == provider_payment_id,
        )
        .with_for_update()
    )
    payment = payment_result.scalar_one_or_none()
    if payment is None:
        raise PaymentNotFound("Payment not found")

    event = PaymentEvent(
        payment_id=payment.id,
        provider=provider,
        event_id=event_id,
        event_type=target_status,
        status="processed",
        payload=payload,
        processed_at=datetime.now(timezone.utc),
    )
    db.add(event)

    if target_status == payment.status:
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            payment = await _payment_for_duplicate_event(
                db,
                provider=provider,
                event_id=event_id,
            )
            if payment is None:
                raise
        await db.refresh(payment)
        return payment

    if target_status not in ALLOWED_TRANSITIONS.get(payment.status, set()):
        current_status = payment.status
        await db.rollback()
        raise PaymentInvalidTransition(
            f"Payment transition {current_status} -> {target_status} is not allowed"
        )

    now = occurred_at or datetime.now(timezone.utc)
    provisioning: ProvisioningResult | None = None
    if target_status == "paid" and payment.subscription_id is None:
        if payment.node_id is None:
            await db.rollback()
            raise PaymentInvalidTransition("Payment has no VPN node")
        try:
            active = (
                await db.execute(
                    select(Subscription)
                    .where(
                        Subscription.user_id == payment.user_id,
                        Subscription.status == "active",
                        Subscription.expires_at > datetime.now(timezone.utc),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if active is not None:
                plan = await db.get(Plan, payment.plan_id)
                if plan is None:
                    raise ProvisioningNotFound("Plan not found")
                provisioning = await renew_paid_subscription(
                    db,
                    subscription=active,
                    plan=plan,
                    node_id=payment.node_id,
                    client_type=payment.client_type,
                    flow=payment.flow,
                    fingerprint=payment.fingerprint,
                    panel_factory=panel_factory,
                )
            else:
                provisioning = await provision_subscription(
                    db,
                    user_id=payment.user_id,
                    plan_id=payment.plan_id,
                    node_id=payment.node_id,
                    client_type=payment.client_type,
                    flow=payment.flow,
                    fingerprint=payment.fingerprint,
                    panel_factory=panel_factory,
                )
        except ProvisioningNotFound as exc:
            await db.rollback()
            raise PaymentNotFound(str(exc)) from exc
        except (ProvisioningConflict, ProvisioningInvalid) as exc:
            await db.rollback()
            raise PaymentInvalidTransition(str(exc)) from exc
        except ProvisioningThreeXUIError as exc:
            await db.rollback()
            raise PaymentProvisioningError(str(exc)) from exc
        except ProvisioningError as exc:
            await db.rollback()
            raise PaymentProvisioningError(str(exc)) from exc
        payment.subscription_id = provisioning.subscription.id

    if target_status == "refunded" and payment.subscription_id is not None:
        subscription = await db.get(Subscription, payment.subscription_id)
        if subscription is not None and subscription.status == "active":
            client_result = await db.execute(
                select(VPNClient).where(
                    VPNClient.subscription_id == subscription.id,
                    VPNClient.status == "active",
                )
            )
            clients = client_result.scalars().all()
            for client in clients:
                revoked = await revoke_vpn_client(
                    db,
                    client,
                    now,
                    panel_factory=panel_factory,
                )
                if not revoked:
                    await db.rollback()
                    raise PaymentProvisioningError(
                        f"Could not revoke VPN client {client.id} for refund"
                    )
            subscription.status = "cancelled"

    payment.status = target_status
    payment.details = {**(payment.details or {}), **payload.get("details", {})}
    if target_status == "paid":
        payment.paid_at = now
    elif target_status == "failed":
        payment.failed_at = now
    elif target_status == "cancelled":
        payment.cancelled_at = now
    elif target_status == "refunded":
        payment.refunded_at = now

    try:
        if provisioning is not None:
            await commit_provisioning(db, provisioning)
        else:
            await db.commit()
    except IntegrityError:
        await db.rollback()
        payment = await _payment_for_duplicate_event(
            db,
            provider=provider,
            event_id=event_id,
        )
        if payment is None:
            raise
    await db.refresh(payment)
    return payment
