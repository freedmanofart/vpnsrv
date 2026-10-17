from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Union
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import APIPrincipal, require_openclaw_assistant
from app.db.models.assistant_action import AssistantPendingAction
from app.db.models.payment import Payment
from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.session import get_db
from app.services.audit import write_audit
from app.services.payments import (
    ALLOWED_TRANSITIONS,
    PaymentError,
    process_payment_event,
)


router = APIRouter(prefix="/assistant", tags=["OpenClaw assistant"])
AssistantAuth = Annotated[APIPrincipal, Depends(require_openclaw_assistant)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaymentStatusPrepare(StrictModel):
    kind: Literal["payment_status"]
    payment_id: int = Field(gt=0)
    status: Literal["failed", "cancelled"]


class PlategaRefundPrepare(StrictModel):
    kind: Literal["platega_refund"]
    payment_id: int = Field(gt=0)


class PlanChanges(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    price: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    duration_days: int | None = Field(default=None, ge=1, le=3650)
    max_connections: int | None = Field(default=None, ge=1, le=100)
    traffic_limit_gb: int | None = Field(default=None, ge=0, le=100_000)
    is_active: bool | None = None
    is_public: bool | None = None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("At least one plan field is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Plan fields cannot be null")
        return self


class PlanUpdatePrepare(StrictModel):
    kind: Literal["plan_update"]
    plan_id: int = Field(gt=0)
    changes: PlanChanges


class NodeChanges(StrictModel):
    status: Literal["active", "disabled"] | None = None
    capacity: int | None = Field(default=None, ge=1, le=100_000)
    region: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("At least one node field is required")
        if any(
            field != "region" and getattr(self, field) is None
            for field in self.model_fields_set
        ):
            raise ValueError("Node status and capacity cannot be null")
        return self


class NodeUpdatePrepare(StrictModel):
    kind: Literal["node_update"]
    node_id: int = Field(gt=0)
    changes: NodeChanges


PrepareData = Annotated[
    Union[
        PaymentStatusPrepare,
        PlategaRefundPrepare,
        PlanUpdatePrepare,
        NodeUpdatePrepare,
    ],
    Field(discriminator="kind"),
]


class ActionApproval(StrictModel):
    confirm_text: str = Field(min_length=1, max_length=100)


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _patch_values(model: BaseModel) -> dict:
    return {
        key: _json_value(value)
        for key, value in model.model_dump(exclude_unset=True).items()
    }


def _plan_fields(plan: Plan, fields: set[str]) -> dict:
    return {field: _json_value(getattr(plan, field)) for field in sorted(fields)}


def _node_fields(node: VPNNode, fields: set[str]) -> dict:
    return {field: _json_value(getattr(node, field)) for field in sorted(fields)}


def _owner_name(principal: APIPrincipal) -> str:
    return str(settings.openclaw_owner_telegram_id or principal.name)


def _platega_headers() -> dict[str, str]:
    if not settings.platega_merchant_id or not settings.platega_secret:
        raise HTTPException(status_code=503, detail="Platega is not configured")
    return {
        "X-MerchantId": settings.platega_merchant_id,
        "X-Secret": settings.platega_secret,
        "Accept": "application/json",
    }


async def _platega_cancel_supported(payment: Payment) -> dict:
    if payment.provider != "platega" or not payment.provider_payment_id:
        raise HTTPException(status_code=409, detail="Refund is available only for Platega payments")
    headers = _platega_headers()
    transaction_id = quote(payment.provider_payment_id, safe="")
    try:
        async with httpx.AsyncClient(
            base_url=settings.platega_base_url.rstrip("/"), timeout=10.0
        ) as client:
            response = await client.get(
                f"/transaction/{transaction_id}/cancel-supported", headers=headers
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not check Platega refund availability") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Platega refund availability check failed")
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Platega returned an invalid refund response") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Platega returned an invalid refund response")
    return result


async def _prepare_action(
    db: AsyncSession,
    *,
    kind: str,
    payload: dict,
    preview: dict,
    principal: APIPrincipal,
) -> dict:
    now = datetime.now(timezone.utc)
    active_count = await db.scalar(
        select(func.count(AssistantPendingAction.id)).where(
            AssistantPendingAction.status == "pending",
            AssistantPendingAction.expires_at > now,
        )
    )
    if (active_count or 0) >= 20:
        raise HTTPException(status_code=429, detail="Too many pending assistant actions")

    action = AssistantPendingAction(
        id=uuid4().hex,
        action_type=kind,
        payload=payload,
        preview=preview,
        status="pending",
        requested_by=_owner_name(principal),
        expires_at=now + timedelta(minutes=10),
    )
    db.add(action)
    await write_audit(
        db,
        action="assistant.action.prepare",
        result="success",
        actor_type="telegram-owner",
        actor_id=_owner_name(principal),
        resource_type="assistant_action",
        resource_id=action.id,
        details={"action_type": kind, "preview": preview},
        commit=False,
    )
    await db.commit()
    return {
        "action_id": action.id,
        "action_type": action.action_type,
        "preview": action.preview,
        "expires_at": action.expires_at,
        "required_confirmation": f"ПОДТВЕРЖДАЮ {action.id}",
        "note": "Изменение не выполнено. Для применения требуется отдельное точное подтверждение владельца.",
    }


@router.get("/users/{telegram_id}")
async def lookup_user(
    telegram_id: int,
    principal: AssistantAuth,
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    subscription = await db.scalar(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    plan = await db.get(Plan, subscription.plan_id) if subscription else None
    await write_audit(
        db,
        action="assistant.user.lookup",
        result="success",
        actor_type="telegram-owner",
        actor_id=_owner_name(principal),
        resource_type="user",
        resource_id=user.id,
        details={"telegram_id": user.telegram_id},
    )
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "status": user.status,
        "created_at": user.created_at,
        "subscription": (
            {
                "id": subscription.id,
                "status": subscription.status,
                "plan": plan.name if plan else None,
                "expires_at": subscription.expires_at,
            }
            if subscription
            else None
        ),
    }


@router.get("/payments")
async def lookup_payments(
    telegram_id: int,
    principal: AssistantAuth,
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    result = await db.execute(
        select(Payment, Plan.name)
        .join(Plan, Plan.id == Payment.plan_id)
        .where(Payment.user_id == user.id)
        .order_by(Payment.id.desc())
        .limit(20)
    )
    rows = result.all()
    await write_audit(
        db,
        action="assistant.payment.lookup",
        result="success",
        actor_type="telegram-owner",
        actor_id=_owner_name(principal),
        resource_type="user",
        resource_id=user.id,
        details={"telegram_id": telegram_id, "payment_count": len(rows)},
    )
    return {
        "telegram_id": telegram_id,
        "payments": [
            {
                "id": payment.id,
                "plan": plan_name,
                "amount": str(payment.amount),
                "currency": payment.currency,
                "status": payment.status,
                "created_at": payment.created_at,
                "paid_at": payment.paid_at,
                "refunded_at": payment.refunded_at,
            }
            for payment, plan_name in rows
        ],
    }


@router.get("/plans")
async def list_plans(
    principal: AssistantAuth,
    db: AsyncSession = Depends(get_db),
):
    plans = (await db.execute(select(Plan).order_by(Plan.id))).scalars().all()
    return {
        "plans": [
            {
                "id": plan.id,
                "code": plan.code,
                "name": plan.name,
                "price": str(plan.price),
                "currency": plan.currency,
                "duration_days": plan.duration_days,
                "max_connections": plan.max_connections,
                "traffic_limit_gb": plan.traffic_limit_gb,
                "is_active": plan.is_active,
                "is_public": plan.is_public,
            }
            for plan in plans
        ]
    }


@router.get("/nodes")
async def list_nodes(
    principal: AssistantAuth,
    db: AsyncSession = Depends(get_db),
):
    nodes = (await db.execute(select(VPNNode).order_by(VPNNode.id))).scalars().all()
    return {
        "nodes": [
            {
                "id": node.id,
                "name": node.name,
                "provider": node.provider,
                "region": node.region,
                "status": node.status,
                "health_status": node.health_status,
                "capacity": node.capacity,
                "active_connections": node.active_connections,
                "latency_ms": node.latency_ms,
                "last_seen_at": node.last_seen_at,
            }
            for node in nodes
        ]
    }


@router.get("/configuration")
async def get_safe_configuration(
    principal: AssistantAuth,
    db: AsyncSession = Depends(get_db),
):
    configured_nodes = set(
        (
            await db.execute(
                select(VPNNodeConfig.node_id).where(VPNNodeConfig.protocol == "vless")
            )
        ).scalars().all()
    )
    nodes = (await db.execute(select(VPNNode).order_by(VPNNode.id))).scalars().all()
    await write_audit(
        db,
        action="assistant.configuration.read",
        result="success",
        actor_type="telegram-owner",
        actor_id=_owner_name(principal),
        resource_type="service_configuration",
        details={"node_count": len(nodes)},
    )
    return {
        "public_base_url": settings.public_base_url,
        "features": {
            "payment_provider": settings.payment_provider,
            "payment_auto_confirm": settings.payment_auto_confirm,
            "platega_enabled": settings.platega_enabled,
            "platega_credentials_configured": bool(
                settings.platega_merchant_id and settings.platega_secret
            ),
            "smtp_configured": bool(settings.smtp_host and settings.smtp_from),
            "background_jobs_enabled": settings.background_jobs_enabled,
            "lifecycle_interval_seconds": settings.lifecycle_interval_seconds,
            "client_trial_enabled": settings.client_trial_enabled,
            "client_trial_days": settings.client_trial_days,
        },
        "nodes": [
            {
                "id": node.id,
                "name": node.name,
                "provider": node.provider,
                "region": node.region,
                "status": node.status,
                "health_status": node.health_status,
                "has_vless_config": node.id in configured_nodes,
            }
            for node in nodes
        ],
        "secrets_included": False,
    }


@router.get("/actions/pending")
async def list_pending_actions(
    principal: AssistantAuth,
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    actions = (
        await db.execute(
            select(AssistantPendingAction)
            .where(
                AssistantPendingAction.status.in_(("pending", "executing", "uncertain", "submitted")),
                (AssistantPendingAction.status != "pending")
                | (AssistantPendingAction.expires_at > now),
            )
            .order_by(AssistantPendingAction.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return {
        "actions": [
            {
                "id": action.id,
                "type": action.action_type,
                "status": action.status,
                "preview": action.preview,
                "outcome": action.outcome,
                "expires_at": action.expires_at,
                "created_at": action.created_at,
            }
            for action in actions
        ]
    }


@router.post("/actions/prepare")
async def prepare_action(
    data: PrepareData,
    principal: AssistantAuth,
    db: AsyncSession = Depends(get_db),
):
    if isinstance(data, PaymentStatusPrepare):
        payment = await db.get(Payment, data.payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.provider == "platega":
            raise HTTPException(status_code=409, detail="Platega payment status is controlled by provider callbacks")
        if not payment.provider_payment_id:
            raise HTTPException(status_code=409, detail="Payment has no provider reference")
        if data.status not in ALLOWED_TRANSITIONS.get(payment.status, set()):
            raise HTTPException(status_code=409, detail="Payment status transition is not allowed")
        return await _prepare_action(
            db,
            kind=data.kind,
            payload={"payment_id": payment.id, "status": data.status, "before": payment.status},
            preview={
                "payment_id": payment.id,
                "amount": str(payment.amount),
                "currency": payment.currency,
                "from_status": payment.status,
                "to_status": data.status,
            },
            principal=principal,
        )

    if isinstance(data, PlategaRefundPrepare):
        payment = await db.get(Payment, data.payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.status != "paid":
            raise HTTPException(status_code=409, detail="Only paid payments can be refunded")
        provider_check = await _platega_cancel_supported(payment)
        if provider_check.get("supported") is not True:
            raise HTTPException(status_code=409, detail="Platega currently does not support a refund for this payment")
        total_deduct = provider_check.get("totalDeductUsdt")
        try:
            if total_deduct is not None:
                parsed_deduction = Decimal(str(total_deduct))
                total_deduct = (
                    str(parsed_deduction)
                    if parsed_deduction.is_finite() and parsed_deduction >= 0
                    else None
                )
        except InvalidOperation:
            total_deduct = None
        return await _prepare_action(
            db,
            kind=data.kind,
            payload={
                "payment_id": payment.id,
                "provider_payment_id": payment.provider_payment_id,
                "before": payment.status,
            },
            preview={
                "payment_id": payment.id,
                "amount": str(payment.amount),
                "currency": payment.currency,
                "status": payment.status,
                "refund_deduction_usdt": total_deduct,
                "refund_supported": True,
                "note": "После подтверждения Platega может отправить возврат на ручную обработку; VPN-доступ отзовётся после подтверждённого статуса CHARGEBACKED.",
            },
            principal=principal,
        )

    if isinstance(data, PlanUpdatePrepare):
        plan = await db.get(Plan, data.plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        changes = _patch_values(data.changes)
        fields = set(changes)
        before = _plan_fields(plan, fields)
        after = dict(before)
        after.update(changes)
        return await _prepare_action(
            db,
            kind=data.kind,
            payload={"plan_id": plan.id, "fields": sorted(fields), "before": before, "changes": changes},
            preview={"plan_id": plan.id, "plan": plan.name, "before": before, "after": after},
            principal=principal,
        )

    if isinstance(data, NodeUpdatePrepare):
        node = await db.get(VPNNode, data.node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Node not found")
        changes = _patch_values(data.changes)
        if "capacity" in changes and changes["capacity"] < node.active_connections:
            raise HTTPException(status_code=409, detail="Node capacity cannot be lower than active connections")
        fields = set(changes)
        before = _node_fields(node, fields)
        after = dict(before)
        after.update(changes)
        warnings = []
        if changes.get("status") == "disabled" and node.active_connections:
            warnings.append(f"У ноды сейчас {node.active_connections} активных подключений.")
        return await _prepare_action(
            db,
            kind=data.kind,
            payload={"node_id": node.id, "fields": sorted(fields), "before": before, "changes": changes},
            preview={"node_id": node.id, "node": node.name, "before": before, "after": after, "warnings": warnings},
            principal=principal,
        )

    raise HTTPException(status_code=422, detail="Unsupported assistant action")


async def _complete_action(
    db: AsyncSession,
    action: AssistantPendingAction,
    *,
    principal: APIPrincipal,
    status: str = "completed",
    outcome: dict | None = None,
) -> None:
    action.status = status
    action.outcome = outcome or {}
    action.confirmed_by = _owner_name(principal)
    action.executed_at = datetime.now(timezone.utc)
    await write_audit(
        db,
        action=f"assistant.action.{status}",
        result="success" if status in {"completed", "submitted"} else "failure",
        actor_type="telegram-owner",
        actor_id=_owner_name(principal),
        resource_type="assistant_action",
        resource_id=action.id,
        details={"action_type": action.action_type, "preview": action.preview, "outcome": outcome or {}},
        commit=False,
    )
    await db.commit()


@router.post("/actions/{action_id}/approve")
async def approve_action(
    action_id: str,
    data: ActionApproval,
    principal: AssistantAuth,
    db: AsyncSession = Depends(get_db),
):
    try:
        action_id = UUID(action_id).hex
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="Pending action not found") from exc
    action = await db.scalar(
        select(AssistantPendingAction)
        .where(AssistantPendingAction.id == action_id)
        .with_for_update()
    )
    if action is None:
        raise HTTPException(status_code=404, detail="Pending action not found")
    if data.confirm_text != f"ПОДТВЕРЖДАЮ {action.id}":
        raise HTTPException(status_code=403, detail="Confirmation text does not match")
    if action.status != "pending":
        raise HTTPException(status_code=409, detail=f"Action is {action.status}; it cannot be repeated")
    now = datetime.now(timezone.utc)
    if action.expires_at <= now:
        action.status = "expired"
        await db.commit()
        raise HTTPException(status_code=410, detail="Pending action has expired")

    if action.action_type == "payment_status":
        payment = await db.get(Payment, int(action.payload["payment_id"]))
        target_status = str(action.payload["status"])
        if payment is None or payment.status != action.payload["before"]:
            action.status = "stale"
            await db.commit()
            raise HTTPException(status_code=409, detail="Payment changed after preview; prepare a new action")
        if payment.provider == "platega" or target_status not in {"failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="This payment status change is not allowed")
        action.status = "executing"
        await db.commit()
        try:
            payment = await process_payment_event(
                db,
                provider=payment.provider,
                event_id=f"openclaw-{action.id}",
                provider_payment_id=payment.provider_payment_id,
                target_status=target_status,
                payload={"details": {"source": "openclaw_assistant", "admin": _owner_name(principal)}},
            )
        except PaymentError as exc:
            current_action = await db.get(AssistantPendingAction, action.id)
            if current_action:
                await _complete_action(db, current_action, principal=principal, status="failed", outcome={"reason": str(exc)[:200]})
            raise HTTPException(status_code=409, detail="Payment status change failed") from exc
        action = await db.get(AssistantPendingAction, action.id)
        await _complete_action(
            db,
            action,
            principal=principal,
            outcome={"payment_id": payment.id, "status": payment.status},
        )
        return {"status": action.status, "outcome": action.outcome}

    if action.action_type in {"plan_update", "node_update"}:
        if action.action_type == "plan_update":
            target = await db.scalar(
                select(Plan)
                .where(Plan.id == int(action.payload["plan_id"]))
                .with_for_update()
            )
            model = Plan
        else:
            target = await db.scalar(
                select(VPNNode)
                .where(VPNNode.id == int(action.payload["node_id"]))
                .with_for_update()
            )
            model = VPNNode
        if target is None:
            action.status = "stale"
            await db.commit()
            raise HTTPException(status_code=409, detail="Target was removed; prepare a new action")
        fields = set(action.payload["fields"])
        current = (
            _plan_fields(target, fields)
            if model is Plan
            else _node_fields(target, fields)
        )
        if current != action.payload["before"]:
            action.status = "stale"
            await db.commit()
            raise HTTPException(status_code=409, detail="Target changed after preview; prepare a new action")
        if model is VPNNode:
            new_capacity = action.payload["changes"].get("capacity")
            if new_capacity is not None and new_capacity < target.active_connections:
                action.status = "stale"
                await db.commit()
                raise HTTPException(status_code=409, detail="Active connections changed; prepare a new capacity")
        for field, value in action.payload["changes"].items():
            if model is Plan and field == "price":
                value = Decimal(value)
            setattr(target, field, value)
        outcome = {
            "target_id": getattr(target, "id"),
            "changes": action.payload["changes"],
        }
        await _complete_action(db, action, principal=principal, outcome=outcome)
        return {"status": action.status, "outcome": action.outcome}

    if action.action_type == "platega_refund":
        payment = await db.get(Payment, int(action.payload["payment_id"]))
        if (
            payment is None
            or payment.provider != "platega"
            or payment.status != "paid"
            or payment.provider_payment_id != action.payload["provider_payment_id"]
        ):
            action.status = "stale"
            await db.commit()
            raise HTTPException(status_code=409, detail="Payment changed after preview; prepare a new action")
        availability = await _platega_cancel_supported(payment)
        if availability.get("supported") is not True:
            action.status = "stale"
            await db.commit()
            raise HTTPException(status_code=409, detail="Platega no longer supports this refund")

        # Lock the action against a second provider cancellation before the network call.
        action.status = "executing"
        await db.commit()
        headers = _platega_headers()
        transaction_id = quote(payment.provider_payment_id, safe="")
        try:
            async with httpx.AsyncClient(
                base_url=settings.platega_base_url.rstrip("/"), timeout=20.0
            ) as client:
                response = await client.post(
                    f"/transaction/{transaction_id}/cancel", headers=headers
                )
        except httpx.HTTPError as exc:
            action = await db.get(AssistantPendingAction, action.id)
            await _complete_action(
                db,
                action,
                principal=principal,
                status="uncertain",
                outcome={"message": "Provider response is unknown; inspect Platega before retrying."},
            )
            raise HTTPException(status_code=502, detail="Refund request outcome is uncertain; inspect Platega before retrying") from exc
        if response.status_code >= 400:
            action = await db.get(AssistantPendingAction, action.id)
            uncertain = response.status_code >= 500
            await _complete_action(
                db,
                action,
                principal=principal,
                status="uncertain" if uncertain else "failed",
                outcome={"provider_http_status": response.status_code},
            )
            if uncertain:
                raise HTTPException(status_code=502, detail="Platega response is uncertain; inspect provider state before retrying")
            raise HTTPException(status_code=502, detail="Platega rejected the refund request")
        try:
            provider_result = response.json()
        except ValueError as exc:
            action = await db.get(AssistantPendingAction, action.id)
            await _complete_action(
                db,
                action,
                principal=principal,
                status="uncertain",
                outcome={"message": "Provider response was invalid; inspect Platega before retrying."},
            )
            raise HTTPException(status_code=502, detail="Platega returned an invalid refund response") from exc
        if not isinstance(provider_result, dict):
            provider_result = {}
        accepted = provider_result.get("accepted") is True
        manual_review = provider_result.get("manualControlRequired") is True
        if manual_review:
            action = await db.get(AssistantPendingAction, action.id)
            await _complete_action(
                db,
                action,
                principal=principal,
                status="submitted",
                outcome={
                    "message": "Refund request submitted to Platega for manual review; payment status will update from the provider callback.",
                    "manual_review": True,
                },
            )
            return {"status": action.status, "outcome": action.outcome}
        if not accepted:
            action = await db.get(AssistantPendingAction, action.id)
            await _complete_action(
                db,
                action,
                principal=principal,
                status="failed",
                outcome={"message": "Platega did not accept the refund request."},
            )
            raise HTTPException(status_code=409, detail="Platega did not accept the refund request")

        try:
            payment = await process_payment_event(
                db,
                provider="platega",
                event_id=f"openclaw-refund-{action.id}",
                provider_payment_id=payment.provider_payment_id,
                target_status="refunded",
                payload={"details": {"source": "openclaw_assistant_refund", "admin": _owner_name(principal)}},
            )
        except PaymentError as exc:
            action = await db.get(AssistantPendingAction, action.id)
            await _complete_action(
                db,
                action,
                principal=principal,
                status="uncertain",
                outcome={"message": "Platega accepted the refund, but VPN/account state needs manual reconciliation."},
            )
            raise HTTPException(status_code=502, detail="Refund accepted; local VPN state needs manual reconciliation") from exc
        action = await db.get(AssistantPendingAction, action.id)
        await _complete_action(
            db,
            action,
            principal=principal,
            outcome={"payment_id": payment.id, "payment_status": payment.status},
        )
        return {"status": action.status, "outcome": action.outcome}

    raise HTTPException(status_code=409, detail="Action type is not supported")
