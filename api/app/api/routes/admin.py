from datetime import datetime, timedelta, timezone
import logging
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.models.payment import Payment
from app.db.models.audit import AuditLog, ClientDevice, DebugSession
from app.db.session import get_db
from app.core.security import APIPrincipal, require_admin
from app.services.audit import write_audit
from app.services.node_health import node_accepts_clients
from app.services.threexui import ThreeXUIClient, ThreeXUIError
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
    fingerprint: str = "chrome"


logger = logging.getLogger(__name__)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _client_link(client: VPNClient, node: VPNNode, config: VPNNodeConfig) -> str:
    if client.config_override:
        return client.config_override
    data = config.config
    host = data.get("host") or node.hostname or node.ip_address
    params = {
        "type": data.get("type", "tcp"),
        "security": data.get("security", "none"),
        "encryption": "none",
        "fp": client.fingerprint or data.get("fp", "chrome"),
    }
    for key in ("sni", "pbk", "sid", "alpn", "path", "serviceName", "mode"):
        if data.get(key) is not None:
            params[key] = data[key]
    transport_host = data.get("xhttp_host") or data.get("host_header")
    if transport_host:
        params["host"] = transport_host
    if client.flow:
        params["flow"] = client.flow
    return (
        f"vless://{client.client_uuid}@{host}:{data.get('port', 443)}"
        f"?{urlencode(params)}#VPN-{client.id}"
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
        "plans": [{"id": x.id, "code": x.code, "name": x.name, "days": x.duration_days, "price": str(x.price), "currency": x.currency, "active": x.is_active} for x in plans],
        "nodes": [{"id": x.id, "name": x.name, "provider": x.provider, "region": x.region, "ip": x.ip_address, "status": x.status, "health": x.health_status, "latency_ms": x.latency_ms, "last_seen_at": x.last_seen_at, "capacity": x.capacity, "connections": x.active_connections} for x in nodes],
        "subscriptions": [{"id": x.id, "user_id": x.user_id, "plan_id": x.plan_id, "status": x.status, "expires_at": x.expires_at} for x in subscriptions],
        "clients": [{"id": x.id, "user_id": x.user_id, "subscription_id": x.subscription_id, "node_id": x.node_id, "client_type": x.client_type, "flow": x.flow, "status": x.status, "expires_at": x.expires_at, "last_connected_at": x.last_connected_at, "last_ip": x.last_ip, "link_overridden": bool(x.config_override)} for x in clients],
        "payments": [{"id": x.id, "user_id": x.user_id, "provider": x.provider, "amount": str(x.amount), "currency": x.currency, "status": x.status, "subscription_id": x.subscription_id, "created_at": x.created_at} for x in payments],
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


ADMIN_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VPN Admin</title><style>
:root{color-scheme:dark;--bg:#0b1020;--card:#141b2d;--line:#29334d;--accent:#61dafb;--bad:#ff6b6b;--ok:#55d187}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf2ff;font:14px system-ui,sans-serif}header{padding:22px 4vw;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}main{padding:24px 4vw;display:grid;gap:22px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}.card,section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}.metric{font-size:28px;font-weight:700;color:var(--accent)}nav{display:flex;gap:8px;flex-wrap:wrap}button{background:#263454;color:white;border:1px solid #3c4c73;border-radius:8px;padding:9px 13px;cursor:pointer}button:hover{border-color:var(--accent)}button.danger{color:#ffb1b1}input,select,textarea{width:100%;background:#0d1425;color:white;border:1px solid var(--line);border-radius:8px;padding:9px}form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}form button{align-self:end}.table{overflow:auto}table{border-collapse:collapse;width:100%;min-width:680px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);max-width:360px;overflow-wrap:anywhere}th{color:#9fb0d0}.hidden{display:none}.ok{color:var(--ok)}.bad{color:var(--bad)}code{color:#b9e9ff}#notice{min-height:20px}dialog{width:min(850px,94vw);background:var(--card);color:#edf2ff;border:1px solid var(--line);border-radius:14px;padding:20px}dialog::backdrop{background:#000a}.wide{grid-column:1/-1}.dialog-actions{display:flex;gap:10px;justify-content:flex-end;grid-column:1/-1}</style></head>
<body><header><div><h1>VPN Admin</h1><div id="notice">Загрузка…</div></div><button onclick="load()">Обновить</button></header><main>
<div class="cards" id="metrics"></div>
<nav id="nav"></nav>
<section id="plans"><h2>Тарифы</h2><form onsubmit="createPlan(event)"><input name="code" placeholder="Код" required><input name="name" placeholder="Название" required><input name="duration_days" type="number" placeholder="Дней" required><input name="price" type="number" step="0.01" placeholder="Цена" required><input name="currency" value="RUB" required><button>Создать тариф</button></form><div class="table"></div></section>
<section id="nodes" class="hidden"><h2>VPN-ноды</h2><form onsubmit="createNode(event)"><input name="name" placeholder="Имя" required><input name="provider" placeholder="Провайдер" required><select name="region"><option value="us">🇺🇸 США</option><option value="nl">🇳🇱 Нидерланды</option><option value="de">🇩🇪 Германия</option></select><input name="ip_address" placeholder="IP" required><input name="hostname" placeholder="Hostname"><input name="capacity" type="number" value="100"><button>Создать ноду</button></form><details><summary>Привязать inbound из 3x-ui master</summary><form onsubmit="createConfig(event)"><input name="node_id" type="number" placeholder="Node ID в VPN Admin" required><input name="api_address" placeholder="https://master.example/base-path" required><input name="host" placeholder="Публичный адрес ноды" required><input name="port" type="number" value="443" required><input name="sni" placeholder="Reality SNI" required><input name="fingerprint" placeholder="Fingerprint" value="chrome" required><input name="pbk" placeholder="Reality public key" required><input name="sid" placeholder="Reality short ID" required><input name="inbound_tag" type="number" min="1" placeholder="3x-ui inbound ID" required><button>Привязать</button></form></details><div class="table"></div></section>
<section id="users" class="hidden"><h2>Пользователи</h2><div class="table"></div></section>
<section id="subscriptions" class="hidden"><h2>Подписки</h2><div class="table"></div></section>
<section id="clients" class="hidden"><h2>VPN-клиенты</h2><div class="table"></div></section>
<section id="payments" class="hidden"><h2>Платежи</h2><div class="table"></div></section>
<section id="devices" class="hidden"><h2>Устройства</h2><div class="table"></div></section>
<section id="debug" class="hidden"><h2>Sensitive debug</h2><p class="bad">Активная сессия разрешает сохранять полные ключи в audit log. После диагностики секреты нужно ротировать.</p><form onsubmit="createDebug(event)"><input name="reason" placeholder="Причина" required><input name="duration_minutes" type="number" min="1" max="1440" value="30" required><button>Открыть сессию</button></form><div class="table"></div></section>
<section id="audit" class="hidden"><h2>Audit log</h2><div class="table"></div></section>
</main>
<dialog id="accessDialog"><h2>Управление доступом</h2><form id="accessForm" onsubmit="saveAccess(event)"><input name="user_id" type="hidden"><label>Тариф<select name="plan_id" required></select></label><label>VPN-нода<select name="node_id" required></select></label><label>Статус<select name="active"><option value="true">Активен</option><option value="false">Не активен</option></select></label><label>Действует до<input name="expires_at" type="datetime-local" required></label><label class="wide">Выданная VPN-ссылка<textarea name="vpn_link" rows="6" placeholder="Пусто — генерировать автоматически"></textarea></label><div class="wide" id="activityInfo"></div><div class="dialog-actions"><button type="button" class="danger" onclick="resetAccess()">Сбросить план и ссылку</button><button type="button" onclick="document.getElementById('accessDialog').close()">Отмена</button><button>Сохранить</button></div></form></dialog><script>
let state={};const sections=['plans','nodes','users','subscriptions','clients','payments','devices','debug','audit'];
function esc(v){if(v!==null&&typeof v==='object')v=JSON.stringify(v);return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function show(id){sections.forEach(x=>document.getElementById(x).classList.toggle('hidden',x!==id))}
function table(id,rows,actions){let keys=rows.length?Object.keys(rows[0]):[];document.querySelector('#'+id+' .table').innerHTML=rows.length?`<table><thead><tr>${keys.map(k=>`<th>${esc(k)}</th>`).join('')}<th></th></tr></thead><tbody>${rows.map(r=>`<tr>${keys.map(k=>`<td>${esc(r[k])}</td>`).join('')}<td>${actions?actions(r):''}</td></tr>`).join('')}</tbody></table>`:'Нет данных'}
async function request(url,opt={}){let r=await fetch(url,opt);if(!r.ok)throw new Error((await r.text())||r.status);return r.status===204?null:r.json()}
async function load(){try{state=await request('/admin/overview');document.getElementById('notice').innerHTML='<span class="ok">API работает</span>';document.getElementById('metrics').innerHTML=sections.map(x=>`<div class="card"><div>${x}</div><div class="metric">${state[x].length}</div></div>`).join('');table('plans',state.plans,r=>`<button onclick="editPlan(${r.id})">Изменить</button>`);table('nodes',state.nodes,r=>`<button onclick="health(${r.id})">Health</button> <button onclick="reconcile(${r.id})">Reconcile</button> <button onclick="editNode(${r.id})">Изменить</button>`);table('users',state.users.map(r=>({...r,vpn_link:r.vpn_link?'выдана':'—'})),r=>`<button onclick="openAccess(${r.id})">Доступ</button> ${r.access_active?`<button onclick="rotateUser(${r.id})">Перевыпустить</button>`:''}`);table('subscriptions',state.subscriptions,r=>`<button onclick="renew(${r.id})">Продлить</button>`);table('clients',state.clients,r=>r.status==='active'?`<button class="danger" onclick="revoke(${r.id})">Отозвать</button>`:'');table('payments',state.payments);table('devices',state.devices,r=>r.status==='active'?`<button class="danger" onclick="revokeDevice(${r.id})">Отозвать</button>`:'');table('debug',state.debug,r=>r.status==='active'?`<button class="danger" onclick="closeDebug(${r.id})">Закрыть</button>`:'');table('audit',state.audit);}catch(e){document.getElementById('notice').innerHTML='<span class="bad">'+esc(e.message)+'</span>'}}
document.getElementById('nav').innerHTML=sections.map(x=>`<button onclick="show('${x}')">${x}</button>`).join('');
async function sendForm(e,url,map){e.preventDefault();let f=Object.fromEntries(new FormData(e.target));for(let k of ['duration_days','price','capacity','node_id','port'])if(k in f)f[k]=Number(f[k]);if(map)f=map(f);try{await request(url(f),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(f)});e.target.reset();await load()}catch(err){alert(err.message)}}
function createPlan(e){return sendForm(e,()=>'/plans')}
function createNode(e){return sendForm(e,()=>'/vpn/nodes')}
async function createConfig(e){e.preventDefault();let f=Object.fromEntries(new FormData(e.target));let id=Number(f.node_id);delete f.node_id;f.port=Number(f.port);f.fp=f.fingerprint;delete f.fingerprint;let body={protocol:'vless',config:{...f,type:'tcp',security:'reality'}};try{await request('/vpn/nodes/'+id+'/configs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await load()}catch(err){alert(err.message)}}
async function renew(id){if(confirm('Продлить подписку #'+id+'?')){await request('/subscriptions/'+id+'/renew',{method:'POST'});await load()}}
async function revoke(id){if(confirm('Отозвать клиент #'+id+'?')){await request('/vpn/clients/'+id,{method:'DELETE'});await load()}}
async function editPlan(id){let p=state.plans.find(x=>x.id===id);let name=prompt('Название тарифа',p.name);if(name===null)return;let price=prompt('Цена',p.price);if(price===null)return;let days=prompt('Длительность, дней',p.days);if(days===null)return;let active=confirm('Тариф должен быть активен?');await request('/plans/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,price:Number(price),duration_days:Number(days),is_active:active})});await load()}
async function editNode(id){let n=state.nodes.find(x=>x.id===id);let region=prompt('Регион: us, nl или de',n.region||'');if(region===null)return;let status=prompt('Статус: active, maintenance, draining, disabled',n.status);if(status===null)return;let capacity=prompt('Ёмкость',n.capacity);if(capacity===null)return;await request('/vpn/nodes/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({region,status,capacity:Number(capacity)})});await load()}
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
