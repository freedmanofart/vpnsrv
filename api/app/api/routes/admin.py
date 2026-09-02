from datetime import datetime, timedelta, timezone
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from fastapi.responses import HTMLResponse, Response as FastAPIResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.models.payment import Payment
from app.db.models.payment_method import PaymentMethod
from app.db.models.audit import AuditLog, ClientDevice, DebugSession
from app.db.session import get_db
from app.core.security import APIPrincipal, require_admin
from app.services.audit import write_audit
from app.services.node_health import node_accepts_clients
from app.services.vless import build_vless_url
from app.services.threexui import ThreeXUIClient, ThreeXUIError
from app.services.payments import PaymentError, process_payment_event
from app.schemas.subscription import VPNClientRotate
from app.api.routes.subscriptions import rotate_subscription_client
from app.core.config import settings

router = APIRouter(prefix="/admin", tags=["Admin"])


class DebugSessionCreate(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
    duration_minutes: int = Field(default=30, ge=1, le=1440)


class DebugSnapshotCreate(BaseModel):
    secrets: dict


class AdminUserAccessUpdate(BaseModel):
    plan_id: int
    node_id: int
    active: bool = True
    expires_at: datetime | None = None
    vpn_link: str | None = Field(default=None, max_length=8192)


class AdminUserRotate(BaseModel):
    node_id: int
    client_type: str = "universal"
    flow: str = ""
    fingerprint: str = "firefox"


class AdminPaymentStatusUpdate(BaseModel):
    status: Literal["paid", "failed", "cancelled", "refunded"]


logger = logging.getLogger(__name__)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _client_link(client: VPNClient, node: VPNNode, config: VPNNodeConfig) -> str:
    if client.config_override:
        return client.config_override
    data = config.config
    host = data.get("host") or node.hostname or node.ip_address
    link_config = dict(data)
    link_config["fp"] = data.get("fp") or client.fingerprint or "firefox"
    if client.flow:
        link_config["flow"] = client.flow
    return build_vless_url(
        uuid=client.client_uuid,
        host=host,
        port=data.get("port", 443),
        config=link_config,
        remark=f"vpn-{client.id}",
    )


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def admin_page(_: APIPrincipal = Depends(require_admin)):
    return HTMLResponse(ADMIN_HTML)


@router.get("/overview", dependencies=[Depends(require_admin)])
async def overview(db: AsyncSession = Depends(get_db)):
    async def rows(model):
        result = await db.execute(select(model).order_by(model.id.desc()).limit(200))
        return result.scalars().all()

    users = await rows(User)
    plans = await rows(Plan)
    nodes = await rows(VPNNode)
    subscriptions = await rows(Subscription)
    clients = await rows(VPNClient)
    payments = await rows(Payment)
    payment_methods = await rows(PaymentMethod)
    devices = await rows(ClientDevice)
    debug_sessions = await rows(DebugSession)
    audit_result = await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(300))
    audit_logs = audit_result.scalars().all()
    config_result = await db.execute(
        select(VPNNodeConfig).where(VPNNodeConfig.protocol == "vless")
    )
    configs = config_result.scalars().all()
    plan_map = {item.id: item for item in plans}
    node_map = {item.id: item for item in nodes}
    config_map = {item.node_id: item for item in configs}
    subscription_map = {}
    for item in subscriptions:
        if item.status in {"active", "disabled"}:
            subscription_map.setdefault(item.user_id, item)
    client_map = {}
    for item in clients:
        if item.status in {"active", "disabled"}:
            client_map.setdefault(item.subscription_id, item)

    user_rows = []
    for user in users:
        subscription = subscription_map.get(user.id)
        plan = plan_map.get(subscription.plan_id) if subscription else None
        client = client_map.get(subscription.id) if subscription else None
        node = node_map.get(client.node_id) if client else None
        config = config_map.get(client.node_id) if client else None
        link = _client_link(client, node, config) if client and node and config else None
        user_rows.append(
            {
                "id": user.id,
                "telegram_id": user.telegram_id,
                "username": user.username,
                "account_status": user.status,
                "access_active": bool(subscription and subscription.status == "active" and client and client.status == "active"),
                "subscription_id": subscription.id if subscription else None,
                "plan_id": plan.id if plan else None,
                "plan": plan.name if plan else None,
                "expires_at": subscription.expires_at if subscription else None,
                "client_id": client.id if client else None,
                "node_id": client.node_id if client else None,
                "client_type": client.client_type if client else None,
                "flow": client.flow if client else None,
                "vpn_link": link,
                "link_overridden": bool(client and client.config_override),
                "last_connected_at": client.last_connected_at if client else None,
                "last_ip": client.last_ip if client else None,
            }
        )
    return {
        "users": user_rows,
        "plans": [{"id": x.id, "code": x.code, "name": x.name, "duration_days": x.duration_days, "max_connections": x.max_connections, "traffic_limit_gb": x.traffic_limit_gb, "price": str(x.price), "currency": x.currency, "active": x.is_active, "public": x.is_public} for x in plans],
        "nodes": [{"id": x.id, "name": x.name, "provider": x.provider, "region": x.region, "ip": x.ip_address, "status": x.status, "health": x.health_status, "latency_ms": x.latency_ms, "last_seen_at": x.last_seen_at, "capacity": x.capacity, "connections": x.active_connections} for x in nodes if x.status != "disabled"],
        "subscriptions": [{"id": x.id, "user_id": x.user_id, "plan_id": x.plan_id, "status": x.status, "expires_at": x.expires_at} for x in subscriptions],
        "clients": [{"id": x.id, "user_id": x.user_id, "subscription_id": x.subscription_id, "node_id": x.node_id, "client_type": x.client_type, "flow": x.flow, "max_connections": x.max_connections, "status": x.status, "expires_at": x.expires_at, "last_connected_at": x.last_connected_at, "last_ip": x.last_ip, "link_overridden": bool(x.config_override)} for x in clients],
        "payments": [{"id": x.id, "user_id": x.user_id, "provider": x.provider, "amount": str(x.amount), "currency": x.currency, "status": x.status, "subscription_id": x.subscription_id, "has_receipt": x.receipt_data is not None, "details": x.details, "created_at": x.created_at} for x in payments],
        "payment_methods": [{"id": x.id, "code": x.code, "name": x.name, "url": x.url, "sort_order": x.sort_order, "is_active": x.is_active, "has_image": x.image_data is not None} for x in sorted(payment_methods, key=lambda item: (item.sort_order, item.id))],
        "devices": [{"id": x.id, "user_id": x.user_id, "name": x.name, "platform": x.platform, "status": x.status, "last_seen_at": x.last_seen_at, "expires_at": x.expires_at} for x in devices],
        "debug": [{"id": x.id, "created_by": x.created_by, "reason": x.reason, "status": x.status, "expires_at": x.expires_at} for x in debug_sessions],
        "audit": [{"id": x.id, "created_at": x.created_at, "actor": f"{x.actor_type}:{x.actor_id or '-'}", "action": x.action, "resource": f"{x.resource_type or '-'}:{x.resource_id or '-'}", "result": x.result, "node_id": x.node_id, "sensitive": x.sensitive, "details": x.details} for x in audit_logs],
    }


@router.put("/users/{user_id}/access", dependencies=[Depends(require_admin)])
async def update_user_access(
    user_id: int,
    data: AdminUserAccessUpdate,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user_result = await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    plan = await db.get(Plan, data.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    node = await db.get(VPNNode, data.node_id)
    if node is None or not node_accepts_clients(
        node, management_mode="threexui"
    ):
        raise HTTPException(status_code=404, detail="Active VPN node not found")
    config_result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == node.id,
            VPNNodeConfig.protocol == "vless",
        )
    )
    node_config = config_result.scalar_one_or_none()
    if node_config is None:
        raise HTTPException(status_code=404, detail="VLESS node configuration not found")

    subscription_result = await db.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(("active", "disabled")),
        )
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    subscription = subscription_result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    expires_at = _aware(data.expires_at) if data.expires_at else now + timedelta(days=plan.duration_days)
    if data.active and expires_at <= now:
        raise HTTPException(status_code=400, detail="Active access must expire in the future")
    target_status = "active" if data.active else "disabled"
    if subscription is None:
        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=target_status,
            starts_at=now,
            expires_at=expires_at,
        )
        db.add(subscription)
        await db.flush()
    else:
        subscription.plan_id = plan.id
        subscription.status = target_status
        subscription.expires_at = expires_at

    client_result = await db.execute(
        select(VPNClient)
        .where(
            VPNClient.subscription_id == subscription.id,
            VPNClient.status.in_(("active", "disabled")),
        )
        .order_by(VPNClient.id.desc())
        .limit(1)
    )
    client = client_result.scalar_one_or_none()
    if client is None:
        client = VPNClient(
            user_id=user.id,
            subscription_id=subscription.id,
            node_id=node.id,
            protocol="vless",
            client_type="universal",
            flow="",
            fingerprint="chrome",
            max_connections=plan.max_connections,
            traffic_limit_gb=plan.traffic_limit_gb,
            client_uuid=str(uuid4()),
            status=target_status,
            expires_at=expires_at,
        )
        db.add(client)
        await db.flush()
        if data.active and not (data.vpn_link or "").strip():
            try:
                await ThreeXUIClient(
                    address=node_config.config.get("api_address")
                ).add_vless_user(
                    inbound_tag=node_config.config.get("inbound_tag"),
                    client_uuid=client.client_uuid,
                    email=f"vpn-{client.id}",
                    flow=client.flow,
                    expiry_time=int(expires_at.timestamp() * 1000),
                    telegram_id=user.telegram_id,
                    limit_ip=client.max_connections,
                    total_gb=client.traffic_limit_gb * 1024 * 1024 * 1024,
                )
            except ThreeXUIError as exc:
                await db.rollback()
                raise HTTPException(
                    status_code=502,
                    detail=f"Не удалось создать ключ в 3x-ui: {exc}",
                ) from exc
    else:
        if client.node_id != node.id:
            raise HTTPException(
                status_code=409,
                detail="Для смены ноды используйте перевыпуск ключа",
            )
        client.node_id = node.id
        client.status = target_status
        client.expires_at = expires_at
        client.revoked_at = None if data.active else now
    client.config_override = (data.vpn_link or "").strip() or None
    await db.commit()
    await write_audit(
        db,
        action="user.access.update",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="user",
        resource_id=user.id,
        node_id=node.id,
        details={
            "plan_id": plan.id,
            "active": data.active,
            "expires_at": expires_at.isoformat(),
            "client_id": client.id,
            "vpn_link_overridden": bool(data.vpn_link),
        },
    )
    return {"user_id": user.id, "subscription_id": subscription.id, "client_id": client.id, "active": data.active}


@router.post("/users/{user_id}/rotate", dependencies=[Depends(require_admin)])
async def rotate_user_key(
    user_id: int,
    data: AdminUserRotate,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == "active",
        )
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        raise HTTPException(status_code=404, detail="Активная подписка не найдена")
    client = await rotate_subscription_client(
        subscription.id,
        VPNClientRotate(
            node_id=data.node_id,
            client_type=data.client_type,
            flow=data.flow,
            fingerprint=data.fingerprint,
        ),
        db,
    )
    await write_audit(
        db,
        action="user.key.rotate",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="user",
        resource_id=user_id,
        node_id=data.node_id,
        details={"client_id": client.id, "subscription_id": subscription.id},
    )
    return {"user_id": user_id, "client_id": client.id, "node_id": client.node_id}


@router.delete("/users/{user_id}/access", dependencies=[Depends(require_admin)])
async def reset_user_access(
    user_id: int,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    now = datetime.now(timezone.utc)
    subscription_result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.status.in_(("active", "disabled")),
        )
    )
    subscriptions = list(subscription_result.scalars())
    subscription_ids = [item.id for item in subscriptions]
    for subscription in subscriptions:
        subscription.status = "expired"
        subscription.expires_at = min(_aware(subscription.expires_at), now)
    reset_clients = 0
    if subscription_ids:
        client_result = await db.execute(
            select(VPNClient).where(VPNClient.subscription_id.in_(subscription_ids))
        )
        for client in client_result.scalars():
            if client.status in {"active", "disabled"}:
                client.status = "revoked"
                client.revoked_at = now
                reset_clients += 1
            client.config_override = None
    await db.commit()
    await write_audit(
        db,
        action="user.access.reset",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="user",
        resource_id=user.id,
        details={"subscriptions": len(subscriptions), "clients": reset_clients},
    )
    return {"user_id": user.id, "reset": True}


@router.post("/debug-sessions", dependencies=[Depends(require_admin)])
async def create_debug_session(
    data: DebugSessionCreate,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    session = DebugSession(
        created_by=principal.name,
        reason=data.reason,
        status="active",
        expires_at=now + timedelta(minutes=data.duration_minutes),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    await write_audit(
        db,
        action="debug_session.create",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="debug_session",
        resource_id=session.id,
        details={"reason": session.reason, "expires_at": session.expires_at.isoformat()},
    )
    return {"id": session.id, "status": session.status, "expires_at": session.expires_at}


@router.delete("/debug-sessions/{session_id}", dependencies=[Depends(require_admin)])
async def close_debug_session(
    session_id: int,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(DebugSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Debug session not found")
    session.status = "closed"
    session.closed_at = datetime.now(timezone.utc)
    await db.commit()
    await write_audit(
        db,
        action="debug_session.close",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="debug_session",
        resource_id=session.id,
    )
    return {"id": session.id, "status": session.status}


@router.post(
    "/debug-sessions/{session_id}/snapshot",
    dependencies=[Depends(require_admin)],
)
async def capture_debug_snapshot(
    session_id: int,
    data: DebugSnapshotCreate,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(DebugSession, session_id)
    now = datetime.now(timezone.utc)
    if (
        session is None
        or session.status != "active"
        or (
            session.expires_at
            if session.expires_at.tzinfo is not None
            else session.expires_at.replace(tzinfo=timezone.utc)
        )
        <= now
    ):
        raise HTTPException(status_code=409, detail="Debug session is not active")
    entry = await write_audit(
        db,
        action="debug_session.sensitive_snapshot",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="debug_session",
        resource_id=session.id,
        sensitive_details={"snapshot": data.secrets},
    )
    logger.warning(
        "sensitive_debug_snapshot",
        extra={
            "allow_sensitive": True,
            "event": {
                "event_type": "sensitive_debug_snapshot",
                "debug_session_id": session.id,
                "audit_log_id": entry.id,
                "snapshot": data.secrets,
            },
        },
    )
    return {"audit_log_id": entry.id, "sensitive": entry.sensitive}


@router.delete("/devices/{device_id}", dependencies=[Depends(require_admin)])
async def revoke_device(
    device_id: int,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    device = await db.get(ClientDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    device.status = "revoked"
    device.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    await write_audit(
        db,
        action="device.revoke",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="client_device",
        resource_id=device.id,
    )
    return {"id": device.id, "status": device.status}


@router.post(
    "/payments/{payment_id}/status",
    dependencies=[Depends(require_admin)],
)
async def update_payment_status(
    payment_id: int,
    data: AdminPaymentStatusUpdate,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    if not payment.provider_payment_id:
        raise HTTPException(status_code=409, detail="Payment has no provider ID")
    try:
        payment = await process_payment_event(
            db,
            provider=payment.provider,
            event_id=f"admin-{payment.id}-{data.status}-{uuid4()}",
            provider_payment_id=payment.provider_payment_id,
            target_status=data.status,
            payload={
                "details": {
                    "source": "vpn-admin",
                    "admin": principal.name,
                }
            },
        )
    except PaymentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await write_audit(
        db,
        action="payment.status.update",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="payment",
        resource_id=payment.id,
        details={"status": payment.status},
    )
    return {"id": payment.id, "status": payment.status}


@router.get("/payments/{payment_id}/receipt", dependencies=[Depends(require_admin)])
async def get_payment_receipt(payment_id: int, db: AsyncSession = Depends(get_db)):
    payment = await db.get(Payment, payment_id)
    if payment is None or payment.receipt_data is None:
        raise HTTPException(status_code=404, detail="Payment receipt not found")
    return FastAPIResponse(
        content=payment.receipt_data,
        media_type=payment.receipt_mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{payment.receipt_filename or "receipt"}"'},
    )


ADMIN_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VPN Admin</title><style>
:root{color-scheme:light;--bg:#f5f8ff;--surface:#fff;--card:#fff;--ink:#111827;--muted:#667085;--line:#e3e9f5;--blue:#175cff;--blue2:#0b49e8;--bad:#d92d20;--ok:#079455;--warn:#b54708;--shadow:0 18px 45px #15336f12}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:sticky;top:0;z-index:3;background:#ffffffd9;backdrop-filter:blur(14px);border-bottom:1px solid var(--line);padding:18px 4vw;display:flex;justify-content:space-between;gap:18px;align-items:center}h1{margin:0;font-size:26px;letter-spacing:-.04em}.subhead{color:var(--muted);margin-top:3px}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.search{min-width:260px}main{padding:24px 4vw 42px;display:grid;grid-template-columns:230px minmax(0,1fr);gap:18px}.cards{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.card,section,nav{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:16px;box-shadow:var(--shadow)}.card{min-height:92px}.card .label{color:var(--muted);font-weight:700}.metric{font-size:30px;font-weight:850;color:var(--blue);letter-spacing:-.05em;margin-top:8px}nav{align-self:start;position:sticky;top:92px;display:grid;gap:8px}button{background:#fff;color:var(--ink);border:1px solid var(--line);border-radius:12px;padding:10px 13px;cursor:pointer;font:inherit;font-weight:750;transition:.15s}button:hover{border-color:var(--blue);box-shadow:0 8px 20px #175cff16;transform:translateY(-1px)}button.primary{background:var(--blue);border-color:var(--blue);color:white}button.active{background:#edf3ff;border-color:#b8ccff;color:var(--blue)}button.danger{color:var(--bad);border-color:#ffd3cc;background:#fff7f5}input,select,textarea{width:100%;background:#fff;color:var(--ink);border:1px solid #d7deea;border-radius:12px;padding:10px 12px;font:inherit}textarea{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}form{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px;margin:12px 0}form button{align-self:end}.table{overflow:auto;border:1px solid var(--line);border-radius:16px;background:#fff}table{border-collapse:separate;border-spacing:0;width:100%;min-width:760px}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);max-width:380px;overflow-wrap:anywhere;vertical-align:top}th{position:sticky;top:0;background:#f8fbff;color:#667085;font-size:12px;text-transform:uppercase;letter-spacing:.04em}tr:hover td{background:#fbfdff}.hidden{display:none}.ok{color:var(--ok);font-weight:800}.bad{color:var(--bad);font-weight:800}code{color:#0b49e8}#notice{min-height:20px;color:var(--muted)}dialog{width:min(900px,94vw);background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:22px;padding:22px;box-shadow:0 30px 80px #0f172a38}dialog::backdrop{background:#0f172a80;backdrop-filter:blur(2px)}.wide{grid-column:1/-1}.dialog-actions{display:flex;gap:10px;justify-content:flex-end;grid-column:1/-1}section>h2{margin-top:0;font-size:24px;letter-spacing:-.03em}section>p{color:var(--muted)}@media(max-width:900px){header{align-items:flex-start;flex-direction:column}.toolbar{width:100%}.search{min-width:0}main{grid-template-columns:1fr}nav{position:static;grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}}</style></head>
<body><header><div><h1>Freedom VPN Admin</h1><div class="subhead">Пользователи, подписки, оплаты, ноды и аудит</div><div id="notice">Загрузка…</div></div><div class="toolbar"><input id="globalSearch" class="search" placeholder="Поиск по текущей таблице" oninput="renderCurrent()"><button class="primary" onclick="load()">Обновить</button></div></header><main>
<div class="cards" id="metrics"></div>
<nav id="nav"></nav>
<section id="plans"><h2>Тарифы</h2><form onsubmit="createPlan(event)"><input name="code" placeholder="Код" required><input name="name" placeholder="Название" required><input name="duration_days" type="number" placeholder="Дней" required><input name="max_connections" type="number" min="0" max="100" value="5" title="0 — без ограничений" required><input name="traffic_limit_gb" type="number" min="0" value="250" title="0 — без ограничений" required><input name="price" type="number" step="0.01" placeholder="Цена" required><input name="currency" value="RUB" required><button>Создать тариф</button></form><p>Лимиты: количество одновременных IP и трафик в ГБ; 0 снимает соответствующее ограничение.</p><div class="table"></div></section>
<section id="nodes" class="hidden"><h2>VPN-ноды</h2><form onsubmit="createNode(event)"><input name="name" placeholder="Имя" required><input name="provider" placeholder="Провайдер" required><input name="ip_address" placeholder="Публичный IP" required><input name="hostname" placeholder="Hostname"><input name="capacity" type="number" value="100"><button>Создать ноду</button></form><p>Страна определяется автоматически по публичному IP.</p><details><summary>Привязать inbound из 3x-ui master</summary><form onsubmit="createConfig(event)"><input name="node_id" type="number" placeholder="Node ID в VPN Admin" required><input name="api_address" placeholder="https://master.example/base-path" required><input name="host" placeholder="Публичный адрес ноды" required><input name="port" type="number" value="443" required><input name="sni" placeholder="Reality SNI" required><input name="fingerprint" placeholder="Fingerprint" value="chrome" required><input name="pbk" placeholder="Reality public key" required><input name="sid" placeholder="Reality short ID" required><input name="inbound_tag" type="number" min="1" placeholder="3x-ui inbound ID" required><button>Привязать</button></form></details><div class="table"></div></section>
<section id="users" class="hidden"><h2>Пользователи</h2><div class="table"></div></section>
<section id="subscriptions" class="hidden"><h2>Подписки</h2><div class="table"></div></section>
<section id="clients" class="hidden"><h2>VPN-клиенты</h2><div class="table"></div></section>
<section id="payments" class="hidden"><h2>Платежи</h2><div class="table"></div></section>
<section id="payment_methods" class="hidden"><h2>Способы оплаты</h2><form onsubmit="createPaymentMethod(event)"><input name="code" placeholder="Код: sber_qr" required><input name="name" placeholder="Название кнопки" required><input name="url" placeholder="Ссылка на QR или реквизиты/телефон"><input name="sort_order" type="number" value="100" required><button>Добавить способ</button></form><p>Для Сбербанка, Т-Банка и перевода по телефону поле URL используется как ссылка на QR или текст реквизитов. Порядок и активность управляют кнопками Telegram.</p><div class="table"></div></section>
<section id="devices" class="hidden"><h2>Устройства</h2><div class="table"></div></section>
<section id="debug" class="hidden"><h2>Sensitive debug</h2><p class="bad">Активная сессия разрешает сохранять полные ключи в audit log. После диагностики секреты нужно ротировать.</p><form onsubmit="createDebug(event)"><input name="reason" placeholder="Причина" required><input name="duration_minutes" type="number" min="1" max="1440" value="30" required><button>Открыть сессию</button></form><div class="table"></div></section>
<section id="audit" class="hidden"><h2>Audit log</h2><div class="table"></div></section>
</main>
<dialog id="accessDialog"><h2>Управление доступом</h2><form id="accessForm" onsubmit="saveAccess(event)"><input name="user_id" type="hidden"><label>Тариф<select name="plan_id" required></select></label><label>VPN-нода<select name="node_id" required></select></label><label>Статус<select name="active"><option value="true">Активен</option><option value="false">Не активен</option></select></label><label>Действует до<input name="expires_at" type="datetime-local" required></label><label class="wide">Выданная VPN-ссылка<textarea name="vpn_link" rows="6" placeholder="Пусто — генерировать автоматически"></textarea></label><div class="wide" id="activityInfo"></div><div class="dialog-actions"><button type="button" class="danger" onclick="resetAccess()">Сбросить план и ссылку</button><button type="button" onclick="document.getElementById('accessDialog').close()">Отмена</button><button>Сохранить</button></div></form></dialog><script>
let state={},current='plans';const sections=['plans','nodes','users','subscriptions','clients','payments','payment_methods','devices','debug','audit'];
const labels={plans:'Тарифы',nodes:'Ноды',users:'Пользователи',subscriptions:'Подписки',clients:'VPN-клиенты',payments:'Платежи',payment_methods:'Способы оплаты',devices:'Устройства',debug:'Debug',audit:'Аудит'};
function esc(v){if(v!==null&&typeof v==='object')v=JSON.stringify(v);return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function show(id){current=id;sections.forEach(x=>document.getElementById(x).classList.toggle('hidden',x!==id));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.section===id));renderCurrent()}
function filtered(rows){let q=(document.getElementById('globalSearch')?.value||'').trim().toLowerCase();return q?rows.filter(r=>JSON.stringify(r).toLowerCase().includes(q)):rows}
function table(id,rows,actions){rows=filtered(rows);let keys=rows.length?Object.keys(rows[0]):[];document.querySelector('#'+id+' .table').innerHTML=rows.length?`<table><thead><tr>${keys.map(k=>`<th>${esc(k)}</th>`).join('')}<th></th></tr></thead><tbody>${rows.map(r=>`<tr>${keys.map(k=>`<td>${esc(r[k])}</td>`).join('')}<td>${actions?actions(r):''}</td></tr>`).join('')}</tbody></table>`:'Нет данных'}
async function request(url,opt={}){let r=await fetch(url,opt);if(!r.ok)throw new Error((await r.text())||r.status);return r.status===204?null:r.json()}
function paymentActions(p){let receipt=p.has_receipt?`<button onclick="window.open('/admin/payments/${p.id}/receipt','_blank')">Открыть чек</button> `:'';if(['pending','processing'].includes(p.status))return receipt+`<button onclick="setPaymentStatus(${p.id},'paid')">Подтвердить</button> <button class="danger" onclick="setPaymentStatus(${p.id},'failed')">Ошибка</button> <button class="danger" onclick="setPaymentStatus(${p.id},'cancelled')">Отменить</button>`;if(p.status==='paid')return receipt+`<button class="danger" onclick="setPaymentStatus(${p.id},'refunded')">Возврат</button>`;return receipt}
function renderCurrent(){if(!state.plans)return;table('plans',state.plans,r=>`<button onclick="editPlan(${r.id})">Изменить</button> <button class="danger" onclick="deletePlan(${r.id})">Удалить</button>`);table('nodes',state.nodes,r=>`<button onclick="health(${r.id})">Health</button> <button onclick="reconcile(${r.id})">Reconcile</button> <button onclick="editNode(${r.id})">Изменить</button>`);table('users',state.users.map(r=>({...r,vpn_link:r.vpn_link?'выдана':'—'})),r=>`<button onclick="openAccess(${r.id})">Доступ</button> ${r.access_active?`<button onclick="rotateUser(${r.id})">Перевыпустить</button>`:''}`);table('subscriptions',state.subscriptions,r=>`<button onclick="renew(${r.id})">Продлить</button>`);table('clients',state.clients,r=>r.status==='active'?`<button class="danger" onclick="revoke(${r.id})">Отозвать</button>`:'');table('payments',state.payments,p=>paymentActions(p));table('payment_methods',state.payment_methods,r=>`<button onclick="editPaymentMethod(${r.id})">Изменить</button> <button onclick="choosePaymentImage(${r.id})">${r.has_image?'Заменить QR':'Загрузить QR'}</button> ${r.has_image?`<button class="danger" onclick="deletePaymentImage(${r.id})">Удалить QR</button>`:''} <button class="danger" onclick="deletePaymentMethod(${r.id})">Удалить</button>`);table('devices',state.devices,r=>r.status==='active'?`<button class="danger" onclick="revokeDevice(${r.id})">Отозвать</button>`:'');table('debug',state.debug,r=>r.status==='active'?`<button class="danger" onclick="closeDebug(${r.id})">Закрыть</button>`:'');table('audit',state.audit)}
async function load(){try{state=await request('/admin/overview');document.getElementById('notice').innerHTML='<span class="ok">API работает</span>';document.getElementById('metrics').innerHTML=sections.map(x=>`<div class="card"><div class="label">${labels[x]}</div><div class="metric">${state[x].length}</div></div>`).join('');renderCurrent();show(current)}catch(e){document.getElementById('notice').innerHTML='<span class="bad">'+esc(e.message)+'</span>'}}
document.getElementById('nav').innerHTML=sections.map(x=>`<button data-section="${x}" onclick="show('${x}')">${labels[x]}</button>`).join('');
async function sendForm(e,url,map){e.preventDefault();let f=Object.fromEntries(new FormData(e.target));for(let k of ['duration_days','max_connections','traffic_limit_gb','price','capacity','node_id','port'])if(k in f)f[k]=Number(f[k]);if(map)f=map(f);try{await request(url(f),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(f)});e.target.reset();await load()}catch(err){alert(err.message)}}
function createPlan(e){return sendForm(e,()=>'/plans')}
function createPaymentMethod(e){return sendForm(e,()=>'/payment-methods',f=>({...f,sort_order:Number(f.sort_order),url:f.url||null,is_active:true}))}
function createNode(e){return sendForm(e,()=>'/vpn/nodes')}
async function createConfig(e){e.preventDefault();let f=Object.fromEntries(new FormData(e.target));let id=Number(f.node_id);delete f.node_id;f.port=Number(f.port);f.fp=f.fingerprint;delete f.fingerprint;let body={protocol:'vless',config:{...f,type:'tcp',security:'reality'}};try{await request('/vpn/nodes/'+id+'/configs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await load()}catch(err){alert(err.message)}}
async function renew(id){if(confirm('Продлить подписку #'+id+'?')){await request('/subscriptions/'+id+'/renew',{method:'POST'});await load()}}
async function revoke(id){if(confirm('Отозвать клиент #'+id+'?')){await request('/vpn/clients/'+id,{method:'DELETE'});await load()}}
async function editPlan(id){let p=state.plans.find(x=>x.id===id);let name=prompt('Название тарифа',p.name);if(name===null)return;let price=prompt('Цена',p.price);if(price===null)return;let days=prompt('Длительность, дней',p.duration_days);if(days===null)return;let limit=prompt('Одновременных подключений/IP; 0 без ограничений',p.max_connections);if(limit===null)return;let traffic=prompt('Трафик, ГБ; 0 без ограничений',p.traffic_limit_gb);if(traffic===null)return;let active=confirm('Тариф должен быть активен и публичен?');await request('/plans/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,price:Number(price),duration_days:Number(days),max_connections:Number(limit),traffic_limit_gb:Number(traffic),is_active:active,is_public:active})});await load()}
async function deletePlan(id){let p=state.plans.find(x=>x.id===id);if(!p)return;if(!confirm(`Удалить тариф «${p.name}» (#${id})?\n\nТариф с подписками или платежами удалить нельзя.`))return;try{await request('/plans/'+id,{method:'DELETE'});await load()}catch(err){alert(err.message)}}
async function setPaymentStatus(id,status){let warnings={paid:'Подтверждение создаст подписку и ключ в 3x-ui.',failed:'Платёж будет отмечен ошибочным.',cancelled:'Платёж будет отменён.',refunded:'Возврат отзовёт активный VPN-ключ и отменит подписку.'};if(!confirm((warnings[status]||'Изменить статус платежа?')+`\n\nПлатёж #${id}, новый статус: ${status}`))return;try{await request('/admin/payments/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});await load()}catch(err){alert(err.message)}}
async function editPaymentMethod(id){let p=state.payment_methods.find(x=>x.id===id);let name=prompt('Название кнопки',p.name);if(name===null)return;let url=prompt('Ссылка оплаты (пусто — без ссылки)',p.url||'');if(url===null)return;let order=prompt('Порядок',p.sort_order);if(order===null)return;let active=confirm('Показывать этот способ в Telegram?');await request('/payment-methods/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,url:url||null,sort_order:Number(order),is_active:active})});await load()}
async function deletePaymentMethod(id){if(!confirm('Удалить способ оплаты #'+id+'?'))return;await request('/payment-methods/'+id,{method:'DELETE'});await load()}
function choosePaymentImage(id){let input=document.createElement('input');input.type='file';input.accept='image/png,image/jpeg,image/webp';input.onchange=()=>uploadPaymentImage(id,input.files[0]);input.click()}
async function uploadPaymentImage(id,file){if(!file)return;if(file.size>4000000){alert('Максимальный размер картинки — 4 МБ');return}let data=await new Promise((resolve,reject)=>{let reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file)});try{await request('/payment-methods/'+id+'/image',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:file.name,mime_type:file.type,data_base64:data})});await load()}catch(err){alert(err.message)}}
async function deletePaymentImage(id){if(!confirm('Удалить QR-картинку?'))return;await request('/payment-methods/'+id+'/image',{method:'DELETE'});await load()}
async function editNode(id){let n=state.nodes.find(x=>x.id===id);let status=prompt('Статус: active, maintenance, draining, disabled',n.status);if(status===null)return;let capacity=prompt('Ёмкость',n.capacity);if(capacity===null)return;await request('/vpn/nodes/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,capacity:Number(capacity)})});await load()}
async function health(id){try{let h=await request('/vpn/nodes/'+id+'/health');alert(h.status==='online'?'Xray online, пользователей: '+h.xray_users:'Xray offline: '+h.error)}catch(e){alert(e.message)}}
async function reconcile(id){try{let r=await request('/vpn/nodes/'+id+'/reconcile',{method:'POST'});alert(`restored=${r.restored}, removed=${r.removed}, errors=${r.errors}`);await load()}catch(e){alert(e.message)}}
async function revokeDevice(id){if(confirm('Отозвать устройство #'+id+'?')){await request('/admin/devices/'+id,{method:'DELETE'});await load()}}
function inputDate(value,days=30){let d=value?new Date(value):new Date(Date.now()+days*86400000);let local=new Date(d.getTime()-d.getTimezoneOffset()*60000);return local.toISOString().slice(0,16)}
function openAccess(id){let u=state.users.find(x=>x.id===id),f=document.getElementById('accessForm');f.user_id.value=id;f.plan_id.innerHTML=state.plans.map(p=>`<option value="${p.id}">${esc(p.name)} (#${p.id})</option>`).join('');f.node_id.innerHTML=state.nodes.filter(n=>n.status==='active').map(n=>`<option value="${n.id}">${esc(n.name)} / ${esc(n.region)}</option>`).join('');if(u.plan_id)f.plan_id.value=u.plan_id;if(u.node_id)f.node_id.value=u.node_id;f.active.value=String(u.access_active);f.expires_at.value=inputDate(u.expires_at);f.vpn_link.value=u.vpn_link||'';document.getElementById('activityInfo').innerHTML=`Последнее подключение: <b>${esc(u.last_connected_at||'нет')}</b> · IP: <b>${esc(u.last_ip||'нет')}</b> · Client ID: <b>${esc(u.client_id||'нет')}</b>${u.link_overridden?' · ссылка изменена вручную':''}`;document.getElementById('accessDialog').showModal()}
async function saveAccess(e){e.preventDefault();let f=Object.fromEntries(new FormData(e.target));let id=Number(f.user_id);let body={plan_id:Number(f.plan_id),node_id:Number(f.node_id),active:f.active==='true',expires_at:new Date(f.expires_at).toISOString(),vpn_link:f.vpn_link};try{await request('/admin/users/'+id+'/access',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});document.getElementById('accessDialog').close();await load()}catch(err){alert(err.message)}}
async function rotateUser(id){let u=state.users.find(x=>x.id===id),available=state.nodes.filter(n=>n.status==='active'&&n.health!=='offline');if(!available.length){alert('Нет доступных нод');return}let hint=available.map(n=>`${n.id}: ${n.name} (${n.region||'—'})`).join('\n');let raw=prompt('ID целевой ноды:\n'+hint,String(u.node_id||available[0].id));if(raw===null)return;let nodeId=Number(raw);if(!available.some(n=>n.id===nodeId)){alert('Нода недоступна');return}if(!confirm('Создать новый ключ на выбранной ноде? Старый будет отозван после успешного создания.'))return;let body={node_id:nodeId,client_type:u.client_type||'universal',flow:u.flow||'',fingerprint:'chrome'};try{await request('/admin/users/'+id+'/rotate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await load();alert('Ключ перевыпущен')}catch(err){alert(err.message)}}
async function resetAccess(){let id=Number(document.getElementById('accessForm').user_id.value);if(!confirm('Сбросить тариф, отключить доступ и удалить ручную ссылку?'))return;try{await request('/admin/users/'+id+'/access',{method:'DELETE'});document.getElementById('accessDialog').close();await load()}catch(err){alert(err.message)}}
async function createDebug(e){return sendForm(e,()=>'/admin/debug-sessions',f=>({...f,duration_minutes:Number(f.duration_minutes)}))}
async function closeDebug(id){if(confirm('Закрыть debug-сессию #'+id+'?')){await request('/admin/debug-sessions/'+id,{method:'DELETE'});await load()}}
load();</script></body></html>"""
