from datetime import datetime, timedelta, timezone

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
from app.db.models.payment import Payment
from app.db.models.audit import AuditLog, ClientDevice, DebugSession
from app.db.session import get_db
from app.core.security import APIPrincipal, require_admin
from app.services.audit import write_audit

router = APIRouter(prefix="/admin", tags=["Admin"])


class DebugSessionCreate(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
    duration_minutes: int = Field(default=30, ge=1, le=1440)


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
    return {
        "users": [{"id": x.id, "telegram_id": x.telegram_id, "username": x.username, "status": x.status} for x in users],
        "plans": [{"id": x.id, "code": x.code, "name": x.name, "days": x.duration_days, "price": str(x.price), "currency": x.currency, "active": x.is_active} for x in plans],
        "nodes": [{"id": x.id, "name": x.name, "provider": x.provider, "region": x.region, "ip": x.ip_address, "status": x.status, "health": x.health_status, "latency_ms": x.latency_ms, "last_seen_at": x.last_seen_at, "capacity": x.capacity, "connections": x.active_connections} for x in nodes],
        "subscriptions": [{"id": x.id, "user_id": x.user_id, "plan_id": x.plan_id, "status": x.status, "expires_at": x.expires_at} for x in subscriptions],
        "clients": [{"id": x.id, "user_id": x.user_id, "subscription_id": x.subscription_id, "node_id": x.node_id, "client_type": x.client_type, "flow": x.flow, "status": x.status, "expires_at": x.expires_at} for x in clients],
        "payments": [{"id": x.id, "user_id": x.user_id, "provider": x.provider, "amount": str(x.amount), "currency": x.currency, "status": x.status, "subscription_id": x.subscription_id, "created_at": x.created_at} for x in payments],
        "devices": [{"id": x.id, "user_id": x.user_id, "name": x.name, "platform": x.platform, "status": x.status, "last_seen_at": x.last_seen_at, "expires_at": x.expires_at} for x in devices],
        "debug": [{"id": x.id, "created_by": x.created_by, "reason": x.reason, "status": x.status, "expires_at": x.expires_at} for x in debug_sessions],
        "audit": [{"id": x.id, "created_at": x.created_at, "actor": f"{x.actor_type}:{x.actor_id or '-'}", "action": x.action, "resource": f"{x.resource_type or '-'}:{x.resource_id or '-'}", "result": x.result, "node_id": x.node_id, "sensitive": x.sensitive, "details": x.details} for x in audit_logs],
    }


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
:root{color-scheme:dark;--bg:#0b1020;--card:#141b2d;--line:#29334d;--accent:#61dafb;--bad:#ff6b6b;--ok:#55d187}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf2ff;font:14px system-ui,sans-serif}header{padding:22px 4vw;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}main{padding:24px 4vw;display:grid;gap:22px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}.card,section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}.metric{font-size:28px;font-weight:700;color:var(--accent)}nav{display:flex;gap:8px;flex-wrap:wrap}button{background:#263454;color:white;border:1px solid #3c4c73;border-radius:8px;padding:9px 13px;cursor:pointer}button:hover{border-color:var(--accent)}button.danger{color:#ffb1b1}input,select,textarea{width:100%;background:#0d1425;color:white;border:1px solid var(--line);border-radius:8px;padding:9px}form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}form button{align-self:end}.table{overflow:auto}table{border-collapse:collapse;width:100%;min-width:680px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line)}th{color:#9fb0d0}.hidden{display:none}.ok{color:var(--ok)}.bad{color:var(--bad)}code{color:#b9e9ff}#notice{min-height:20px}</style></head>
<body><header><div><h1>VPN Admin</h1><div id="notice">Загрузка…</div></div><button onclick="load()">Обновить</button></header><main>
<div class="cards" id="metrics"></div>
<nav id="nav"></nav>
<section id="plans"><h2>Тарифы</h2><form onsubmit="createPlan(event)"><input name="code" placeholder="Код" required><input name="name" placeholder="Название" required><input name="duration_days" type="number" placeholder="Дней" required><input name="price" type="number" step="0.01" placeholder="Цена" required><input name="currency" value="RUB" required><button>Создать тариф</button></form><div class="table"></div></section>
<section id="nodes" class="hidden"><h2>VPN-ноды</h2><form onsubmit="createNode(event)"><input name="name" placeholder="Имя" required><input name="provider" placeholder="Провайдер" required><select name="region"><option value="us">🇺🇸 США</option><option value="nl">🇳🇱 Нидерланды</option><option value="de">🇩🇪 Германия</option></select><input name="ip_address" placeholder="IP" required><input name="hostname" placeholder="Hostname"><input name="capacity" type="number" value="100"><button>Создать ноду</button></form><details><summary>Добавить VLESS Reality конфигурацию</summary><form onsubmit="createConfig(event)"><input name="node_id" type="number" placeholder="Node ID" required><input name="api_address" placeholder="Tailscale gRPC host:port" required><input name="host" placeholder="Публичный host" required><input name="port" type="number" value="443" required><input name="sni" placeholder="SNI" required><input name="pbk" placeholder="Public key" required><input name="sid" placeholder="Short ID" required><input name="inbound_tag" value="vless-reality"><button>Добавить</button></form></details><div class="table"></div></section>
<section id="users" class="hidden"><h2>Пользователи</h2><div class="table"></div></section>
<section id="subscriptions" class="hidden"><h2>Подписки</h2><div class="table"></div></section>
<section id="clients" class="hidden"><h2>VPN-клиенты</h2><div class="table"></div></section>
<section id="payments" class="hidden"><h2>Платежи</h2><div class="table"></div></section>
<section id="devices" class="hidden"><h2>Устройства</h2><div class="table"></div></section>
<section id="debug" class="hidden"><h2>Sensitive debug</h2><p class="bad">Активная сессия разрешает сохранять полные ключи в audit log. После диагностики секреты нужно ротировать.</p><form onsubmit="createDebug(event)"><input name="reason" placeholder="Причина" required><input name="duration_minutes" type="number" min="1" max="1440" value="30" required><button>Открыть сессию</button></form><div class="table"></div></section>
<section id="audit" class="hidden"><h2>Audit log</h2><div class="table"></div></section>
</main><script>
let state={};const sections=['plans','nodes','users','subscriptions','clients','payments','devices','debug','audit'];
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function show(id){sections.forEach(x=>document.getElementById(x).classList.toggle('hidden',x!==id))}
function table(id,rows,actions){let keys=rows.length?Object.keys(rows[0]):[];document.querySelector('#'+id+' .table').innerHTML=rows.length?`<table><thead><tr>${keys.map(k=>`<th>${esc(k)}</th>`).join('')}<th></th></tr></thead><tbody>${rows.map(r=>`<tr>${keys.map(k=>`<td>${esc(r[k])}</td>`).join('')}<td>${actions?actions(r):''}</td></tr>`).join('')}</tbody></table>`:'Нет данных'}
async function request(url,opt={}){let r=await fetch(url,opt);if(!r.ok)throw new Error((await r.text())||r.status);return r.status===204?null:r.json()}
async function load(){try{state=await request('/admin/overview');document.getElementById('notice').innerHTML='<span class="ok">API работает</span>';document.getElementById('metrics').innerHTML=sections.map(x=>`<div class="card"><div>${x}</div><div class="metric">${state[x].length}</div></div>`).join('');table('plans',state.plans,r=>`<button onclick="editPlan(${r.id})">Изменить</button>`);table('nodes',state.nodes,r=>`<button onclick="health(${r.id})">Health</button> <button onclick="reconcile(${r.id})">Reconcile</button> <button onclick="editNode(${r.id})">Изменить</button>`);table('users',state.users);table('subscriptions',state.subscriptions,r=>`<button onclick="renew(${r.id})">Продлить</button>`);table('clients',state.clients,r=>r.status==='active'?`<button class="danger" onclick="revoke(${r.id})">Отозвать</button>`:'');table('payments',state.payments);table('devices',state.devices,r=>r.status==='active'?`<button class="danger" onclick="revokeDevice(${r.id})">Отозвать</button>`:'');table('debug',state.debug,r=>r.status==='active'?`<button class="danger" onclick="closeDebug(${r.id})">Закрыть</button>`:'');table('audit',state.audit);}catch(e){document.getElementById('notice').innerHTML='<span class="bad">'+esc(e.message)+'</span>'}}
document.getElementById('nav').innerHTML=sections.map(x=>`<button onclick="show('${x}')">${x}</button>`).join('');
async function sendForm(e,url,map){e.preventDefault();let f=Object.fromEntries(new FormData(e.target));for(let k of ['duration_days','price','capacity','node_id','port'])if(k in f)f[k]=Number(f[k]);if(map)f=map(f);try{await request(url(f),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(f)});e.target.reset();await load()}catch(err){alert(err.message)}}
function createPlan(e){return sendForm(e,()=>'/plans')}
function createNode(e){return sendForm(e,()=>'/vpn/nodes')}
async function createConfig(e){e.preventDefault();let f=Object.fromEntries(new FormData(e.target));let id=Number(f.node_id);delete f.node_id;f.port=Number(f.port);let body={protocol:'vless',config:{...f,type:'tcp',security:'reality',fp:'chrome'}};try{await request('/vpn/nodes/'+id+'/configs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await load()}catch(err){alert(err.message)}}
async function renew(id){if(confirm('Продлить подписку #'+id+'?')){await request('/subscriptions/'+id+'/renew',{method:'POST'});await load()}}
async function revoke(id){if(confirm('Отозвать клиент #'+id+'?')){await request('/vpn/clients/'+id,{method:'DELETE'});await load()}}
async function editPlan(id){let p=state.plans.find(x=>x.id===id);let name=prompt('Название тарифа',p.name);if(name===null)return;let price=prompt('Цена',p.price);if(price===null)return;let days=prompt('Длительность, дней',p.days);if(days===null)return;let active=confirm('Тариф должен быть активен?');await request('/plans/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,price:Number(price),duration_days:Number(days),is_active:active})});await load()}
async function editNode(id){let n=state.nodes.find(x=>x.id===id);let region=prompt('Регион: us, nl или de',n.region||'');if(region===null)return;let status=prompt('Статус: active, maintenance, draining, disabled',n.status);if(status===null)return;let capacity=prompt('Ёмкость',n.capacity);if(capacity===null)return;await request('/vpn/nodes/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({region,status,capacity:Number(capacity)})});await load()}
async function health(id){try{let h=await request('/vpn/nodes/'+id+'/health');alert(h.status==='online'?'Xray online, пользователей: '+h.xray_users:'Xray offline: '+h.error)}catch(e){alert(e.message)}}
async function reconcile(id){try{let r=await request('/vpn/nodes/'+id+'/reconcile',{method:'POST'});alert(`restored=${r.restored}, removed=${r.removed}, errors=${r.errors}`);await load()}catch(e){alert(e.message)}}
async function revokeDevice(id){if(confirm('Отозвать устройство #'+id+'?')){await request('/admin/devices/'+id,{method:'DELETE'});await load()}}
async function createDebug(e){return sendForm(e,()=>'/admin/debug-sessions',f=>({...f,duration_minutes:Number(f.duration_minutes)}))}
async function closeDebug(id){if(confirm('Закрыть debug-сессию #'+id+'?')){await request('/admin/debug-sessions/'+id,{method:'DELETE'});await load()}}
load();</script></body></html>"""
