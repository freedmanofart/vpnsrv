from __future__ import annotations

import html
import re
import secrets
import base64
import binascii
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.client import build_client_uri
from app.core.config import settings
from app.db.models.cabinet_access import CabinetAccessToken
from app.db.models.plan import Plan
from app.db.models.payment import Payment
from app.db.models.payment_method import PaymentMethod
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.session import get_db
from app.services.email import EmailDeliveryError, send_cabinet_link
from app.services.payments import PaymentError, create_payment
from app.schemas.payment import PaymentCreate


router = APIRouter(tags=["Web cabinet"])
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
COOKIE = "freedom_cabinet"


class Registration(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    plan_id: int | None = None


class WebOrder(BaseModel):
    plan_id: int
    node_id: int
    method_code: str = Field(min_length=2, max_length=64)


class WebReceipt(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(pattern=r"^image/(png|jpeg|webp)$|^application/pdf$")
    data_base64: str = Field(min_length=4, max_length=12_000_000)


def _digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    }


async def _plans(db: AsyncSession) -> list[Plan]:
    result = await db.execute(
        select(Plan).where(Plan.is_active.is_(True), Plan.is_public.is_(True)).order_by(Plan.price, Plan.duration_days)
    )
    return list(result.scalars())


def _plan_cards(plans: list[Plan]) -> str:
    cards = []
    icons = {"lite": "🛴", "standard": "🔥", "ultra": "🚀"}
    for plan in plans:
        tier = plan.code.split("-")[0].lower()
        devices = "без ограничений" if not plan.max_connections else str(plan.max_connections)
        traffic = "без ограничений" if not plan.traffic_limit_gb else f"{plan.traffic_limit_gb} ГБ"
        cards.append(
            f'<article class="plan"><span class="pill">{icons.get(tier, "✦")} {html.escape(plan.name)}</span>'
            f'<div class="price">{plan.price:g} ₽</div><div class="muted">за {plan.duration_days} дн.</div>'
            f'<ul><li>До {devices} одновременных подключений</li><li>{traffic} трафика</li><li>Все поддерживаемые устройства</li></ul>'
            f'<button class="button primary" data-plan="{plan.id}">Выбрать</button></article>'
        )
    return "".join(cards) or '<p class="muted">Публичные тарифы временно недоступны.</p>'


def _shell(content: str, *, title: str = "Freedom VPN") -> str:
    return f"""<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"referrer\" content=\"no-referrer\"><title>{html.escape(title)}</title><style>
:root{{--navy:#061541;--blue:#175cff;--green:#0da878;--ink:#11182b;--muted:#72809a;--line:#e7ebf3;--bg:#f6f8fc}}*{{box-sizing:border-box}}body{{margin:0;font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:white}}a{{color:inherit;text-decoration:none}}.wrap{{max-width:1180px;margin:auto;padding:0 28px}}nav{{height:76px;display:flex;align-items:center;justify-content:space-between}}.brand{{font-size:21px;font-weight:850;letter-spacing:-.04em;display:flex;align-items:center;gap:10px}}.brand img{{width:38px;height:38px;border-radius:10px}}.brand i{{font-style:normal;color:var(--blue)}}.links{{display:flex;gap:26px;align-items:center}}.button{{border:1px solid #d9dfeb;border-radius:12px;padding:12px 18px;background:#fff;font:inherit;font-weight:750;cursor:pointer}}.primary{{background:var(--blue);border-color:var(--blue);color:#fff}}.hero{{background:var(--navy);color:white;border-radius:0 0 28px 28px;padding:78px 0;overflow:hidden}}.hero-grid{{display:grid;grid-template-columns:1.05fr .95fr;gap:48px;align-items:center}}h1{{font-size:clamp(44px,6vw,76px);line-height:.98;letter-spacing:-.065em;margin:15px 0 25px}}.lead{{font-size:18px;color:#bdc9e8;max-width:620px}}.eyebrow{{color:#76a0ff;text-transform:uppercase;letter-spacing:.16em;font-size:12px;font-weight:800}}.preview,.panel{{background:#fff;color:var(--ink);border-radius:22px;padding:24px;box-shadow:0 25px 75px #0003}}.preview-logo{{width:100%;max-height:320px;object-fit:cover;border-radius:16px;margin-bottom:18px}}.key{{background:#f2f5fa;border:1px solid var(--line);padding:18px;border-radius:14px;overflow:hidden}}.key code{{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}}.stat{{border:1px solid var(--line);border-radius:13px;padding:13px}}.muted{{color:var(--muted)}}section{{padding:78px 0}}h2{{font-size:42px;letter-spacing:-.05em;margin:0 0 12px}}.plans{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:28px}}.plan{{border:1px solid var(--line);border-radius:19px;padding:21px;background:white}}.plan .price{{font-size:30px;font-weight:850;margin-top:25px}}.pill{{font-weight:800}}.plan ul{{padding-left:19px;color:#536078;min-height:105px}}.plan .button{{width:100%}}.alt{{background:var(--bg)}}.features{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.feature{{background:white;border:1px solid var(--line);border-radius:17px;padding:22px}}footer{{padding:36px 0;color:var(--muted)}}.modal{{display:none;position:fixed;inset:0;background:#0c173e99;z-index:5;align-items:center;justify-content:center;padding:20px}}.modal.open{{display:flex}}.modal-card{{background:white;border-radius:20px;width:min(480px,100%);padding:25px}}input,select{{width:100%;padding:13px;border:1px solid #d9dfeb;border-radius:11px;font:inherit;margin:7px 0}}.error{{color:#c83232}}.success{{color:var(--green)}}.cabinet{{padding:48px 0 80px}}.cabinet-grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}}.panel{{box-shadow:none;border:1px solid var(--line)}}.status{{color:var(--green);font-weight:800}}.actions{{display:flex;gap:9px;flex-wrap:wrap;margin-top:18px}}@media(max-width:760px){{.links a:not(.button){{display:none}}.hero-grid,.plans,.features,.cabinet-grid{{grid-template-columns:1fr}}.hero{{padding:50px 0}}.stats{{grid-template-columns:1fr}}}}
</style></head><body>{content}</body></html>"""


@router.get("/", response_class=HTMLResponse)
async def landing(db: AsyncSession = Depends(get_db)):
    plans = await _plans(db)
    body = f"""<div class=\"wrap\"><nav><a class=\"brand\" href=\"/\"><img src=\"/static/freedom-vpn-logo.png\" alt=\"\">Freedom <i>VPN</i></a><div class=\"links\"><a href=\"#advantages\">Возможности</a><a href=\"#plans\">Тарифы</a><a class=\"button\" href=\"/cabinet\">Войти</a></div></nav></div>
<header class=\"hero\"><div class=\"wrap hero-grid\"><div><div class=\"eyebrow\">Доступ к интернету без зависимости от Telegram</div><h1>Свобода подключения на всех устройствах</h1><p class=\"lead\">Оформляйте подписку и сохраняйте резервный доступ к VPN в защищённом веб-кабинете.</p><div class=\"actions\"><a class=\"button primary\" href=\"#plans\">Выбрать тариф</a><a class=\"button\" href=\"/cabinet\">Управление подпиской</a></div></div><div class=\"preview\"><img class=\"preview-logo\" src=\"/static/freedom-vpn-logo.png\" alt=\"Freedom VPN\"><p class=\"status\">● Защищённое подключение</p><div class=\"key\"><span class=\"muted\">Ключ доступа</span><code>vless://••••••••••••••••••••</code></div><div class=\"stats\"><div class=\"stat\"><small class=\"muted\">Локация</small><br><b>Вы выбираете</b></div><div class=\"stat\"><small class=\"muted\">Устройства</small><br><b>По тарифу</b></div><div class=\"stat\"><small class=\"muted\">Доступ</small><br><b>24/7</b></div></div></div></div></header>
<main><section id=\"plans\"><div class=\"wrap\"><div class=\"eyebrow\">Тарифы из административной панели</div><h2>Выберите свой формат</h2><p class=\"muted\">Цена, срок, трафик и число подключений синхронизированы с VPN API.</p><div class=\"plans\">{_plan_cards(plans)}</div></div></section><section id=\"advantages\" class=\"alt\"><div class=\"wrap features\"><article class=\"feature\"><h3>Резервный вход</h3><p class=\"muted\">Персональная ссылка приходит на почту и работает независимо от Telegram.</p></article><article class=\"feature\"><h3>Один источник данных</h3><p class=\"muted\">Подписка, лимиты и VPN-ключ берутся из общей базы и 3x-ui.</p></article><article class=\"feature\"><h3>Для всех устройств</h3><p class=\"muted\">iOS, Android, Windows, macOS и роутеры.</p></article></div></section></main><footer><div class=\"wrap\">Freedom VPN · Управление подпиской даже при недоступности Telegram</div></footer>
<div class=\"modal\" id=\"register\"><div class=\"modal-card\"><h3>Создать веб-кабинет</h3><p class=\"muted\">Ссылка для входа будет отправлена на указанную почту.</p><input id=\"email\" type=\"email\" autocomplete=\"email\" placeholder=\"you@example.com\"><input id=\"plan\" type=\"hidden\"><p id=\"result\"></p><div class=\"actions\"><button class=\"button\" onclick=\"closeModal()\">Отмена</button><button class=\"button primary\" onclick=\"register()\">Отправить ссылку</button></div></div></div>
<script>const modal=document.getElementById('register');document.querySelectorAll('[data-plan]').forEach(b=>b.onclick=()=>{{document.getElementById('plan').value=b.dataset.plan;modal.classList.add('open')}});function closeModal(){{modal.classList.remove('open')}}async function register(){{const out=document.getElementById('result');out.className='';out.textContent='Отправляем…';const r=await fetch('/web/register',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:document.getElementById('email').value,plan_id:Number(document.getElementById('plan').value)||null}})}});const d=await r.json();out.className=r.ok?'success':'error';out.textContent=r.ok?d.message:(d.detail||'Ошибка регистрации')}};</script>"""
    return HTMLResponse(_shell(body), headers=_headers())


@router.post("/web/register")
async def register(data: Registration, db: AsyncSession = Depends(get_db)):
    email_address = data.email.strip().lower()
    if not EMAIL_RE.fullmatch(email_address):
        raise HTTPException(status_code=422, detail="Укажите корректный email")
    if data.plan_id is not None:
        plan = await db.get(Plan, data.plan_id)
        if plan is None or not plan.is_active or not plan.is_public:
            raise HTTPException(status_code=404, detail="Тариф не найден")
    user = await db.scalar(select(User).where(User.email == email_address))
    if user is None:
        # Telegram IDs are positive. A random negative service identifier keeps the
        # existing 3x-ui provisioning contract compatible for web-only accounts.
        user = User(telegram_id=-secrets.randbelow(9_000_000_000_000_000) - 1, email=email_address, status="active")
        db.add(user)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            user = await db.scalar(select(User).where(User.email == email_address))
            if user is None:
                raise HTTPException(status_code=409, detail="Не удалось создать пользователя")
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.cabinet_token_ttl_days)
    access = CabinetAccessToken(user_id=user.id, token_hash=_digest(raw), expires_at=expires)
    db.add(access)
    base = settings.public_base_url.rstrip("/")
    link = f"{base}/cabinet/access/{raw}"
    try:
        await send_cabinet_link(email_address, link)
    except EmailDeliveryError as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await db.commit()
    return {"message": "Ссылка для входа отправлена на почту", "expires_at": expires}


async def _access(raw: str | None, db: AsyncSession) -> tuple[User, CabinetAccessToken] | None:
    if not raw:
        return None
    access = await db.scalar(select(CabinetAccessToken).where(CabinetAccessToken.token_hash == _digest(raw)))
    now = datetime.now(timezone.utc)
    if access is None or access.revoked_at is not None or access.expires_at.replace(tzinfo=access.expires_at.tzinfo or timezone.utc) <= now:
        return None
    user = await db.get(User, access.user_id)
    return (user, access) if user and user.status == "active" else None


async def _require_cabinet(raw: str | None, db: AsyncSession) -> tuple[User, CabinetAccessToken]:
    found = await _access(raw, db)
    if found is None:
        raise HTTPException(status_code=401, detail="Требуется вход в кабинет")
    return found


@router.get("/cabinet/access/{token}")
async def cabinet_access(token: str, db: AsyncSession = Depends(get_db)):
    found = await _access(token, db)
    if found is None:
        return HTMLResponse(_shell('<main class="wrap cabinet"><div class="panel"><h2>Ссылка недействительна</h2><p class="muted">Запросите новую ссылку на главной странице.</p><a class="button primary" href="/">На главную</a></div></main>'), status_code=401, headers=_headers())
    _, access = found
    access.last_used_at = datetime.now(timezone.utc)
    await db.commit()
    response = RedirectResponse("/cabinet", status_code=303, headers=_headers())
    secure = urlparse(settings.public_base_url).scheme == "https"
    response.set_cookie(COOKIE, token, max_age=settings.cabinet_token_ttl_days * 86400, httponly=True, secure=secure, samesite="strict", path="/")
    return response


@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(cabinet_token: str | None = Cookie(default=None, alias=COOKIE), db: AsyncSession = Depends(get_db)):
    found = await _access(cabinet_token, db)
    if found is None:
        body = '<div class="wrap"><nav><a class="brand" href="/"><img src="/static/freedom-vpn-logo.png" alt="">Freedom <i>VPN</i></a></nav><main class="cabinet"><div class="panel"><h2>Вход в кабинет</h2><p class="muted">Откройте персональную ссылку из письма. Если ссылки ещё нет, выберите тариф и зарегистрируйтесь.</p><a class="button primary" href="/#plans">Получить ссылку</a></div></main></div>'
        return HTMLResponse(_shell(body, title="Вход — Freedom VPN"), status_code=401, headers=_headers())
    user, access = found
    access.last_used_at = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    subscription = (await db.execute(select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.id.desc()))).scalars().first()
    plan = await db.get(Plan, subscription.plan_id) if subscription else None
    client = None
    vpn_uri = ""
    node = None
    if subscription:
        client = (await db.execute(select(VPNClient).where(VPNClient.subscription_id == subscription.id, VPNClient.status == "active").order_by(VPNClient.id.desc()))).scalars().first()
    if client:
        node = await db.get(VPNNode, client.node_id)
        config = await db.scalar(select(VPNNodeConfig).where(VPNNodeConfig.node_id == client.node_id, VPNNodeConfig.protocol == client.protocol))
        if node and config:
            vpn_uri = client.config_override or build_client_uri(client, node, config.config)
    active = bool(subscription and subscription.status == "active" and subscription.expires_at.replace(tzinfo=subscription.expires_at.tzinfo or timezone.utc) > now)
    days = max(0, int(((subscription.expires_at.replace(tzinfo=subscription.expires_at.tzinfo or timezone.utc) - now).total_seconds() + 86399) // 86400)) if subscription else 0
    traffic = "Без ограничений" if client and not client.traffic_limit_gb else (f"{client.traffic_limit_gb} ГБ" if client else "—")
    devices = "Без ограничений" if client and not client.max_connections else (str(client.max_connections) if client else "—")
    public_plans = await _plans(db)
    nodes = list((await db.execute(select(VPNNode).where(VPNNode.status == "active").order_by(VPNNode.name))).scalars())
    methods = list((await db.execute(select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).order_by(PaymentMethod.sort_order))).scalars())
    payments = list((await db.execute(select(Payment).where(Payment.user_id == user.id).order_by(Payment.id.desc()).limit(10))).scalars())
    plan_options = "".join(f'<option value="{p.id}">{html.escape(p.name)} · {p.duration_days} дн. · {p.price:g} ₽</option>' for p in public_plans)
    node_options = "".join(f'<option value="{n.id}">{html.escape(n.region or n.name)}</option>' for n in nodes)
    method_options = "".join(f'<option value="{html.escape(m.code)}">{html.escape(m.name)}</option>' for m in methods)
    payment_rows = "".join(f'<li>Платёж #{p.id}: {p.amount:g} {html.escape(p.currency)} — <b>{html.escape(p.status)}</b></li>' for p in payments) or "<li>Платежей ещё нет</li>"
    key_block = '<p class="muted">Ключ появится после подтверждения оплаты и выдачи подписки.</p>'
    if vpn_uri:
        key_block = (
            f'<div class="key"><code id="vpn-key">{html.escape(vpn_uri)}</code></div>'
            '<div class="actions"><button class="button" onclick="copyKey(this)">Копировать ключ</button></div>'
        )
    body = f'''<div class="wrap"><nav><a class="brand" href="/">Freedom <i>VPN</i></a><div class="links"><a href="/#plans">Тарифы</a><form method="post" action="/cabinet/logout"><button class="button">Выйти</button></form></div></nav><main class="cabinet"><div class="eyebrow">Управление подпиской</div><h2>{html.escape(user.email or "Ваш кабинет")}</h2><div class="cabinet-grid"><section class="panel"><h3>Подписка <span class="status">{'активна' if active else 'не активна'}</span></h3><p><b>{html.escape(plan.name) if plan else 'Тариф не выбран'}</b></p><div class="stats"><div class="stat"><small class="muted">Осталось</small><br><b>{days} дн.</b></div><div class="stat"><small class="muted">Трафик</small><br><b>{traffic}</b></div><div class="stat"><small class="muted">Подключения</small><br><b>{devices}</b></div></div></section><section class="panel"><h3>Ключ доступа</h3>{key_block}<p class="muted">Локация: {html.escape(node.region or node.name) if node else '—'}</p></section></div><section><h2>Приобрести или продлить</h2><div class="cabinet-grid"><div class="panel"><label>Тариф<select id="order-plan">{plan_options}</select></label><label>Страна<select id="order-node">{node_options}</select></label><label>Способ оплаты<select id="order-method">{method_options}</select></label><button class="button primary" onclick="createOrder()">Создать платёж</button><div id="order-result"></div></div><div class="panel"><h3>Последние платежи</h3><ul>{payment_rows}</ul></div></div></section><section><h2>Приложения</h2><div class="actions"><a class="button" href="https://github.com/amnezia-vpn/amnezia-client/releases/download/4.8.10.0/AmneziaVPN_4.8.10.0_windows_x64.exe">Windows</a><a class="button" href="https://github.com/amnezia-vpn/amnezia-client/releases/download/4.8.10.0/AmneziaVPN_4.8.10.0_macos.zip">macOS</a><a class="button" href="https://play.google.com/store/apps/details?id=org.amnezia.vpn">Android</a><a class="button" href="https://apps.apple.com/ru/app/defaultvpn/id6744725017">iOS</a></div></section></main></div><script>function copyKey(button){{navigator.clipboard.writeText(document.getElementById('vpn-key').textContent);button.textContent='Скопировано ✓'}}async function createOrder(){{const out=document.getElementById('order-result');out.textContent='Создаём платёж…';const r=await fetch('/web/payments/manual',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{plan_id:Number(document.getElementById('order-plan').value),node_id:Number(document.getElementById('order-node').value),method_code:document.getElementById('order-method').value}})}});const d=await r.json();if(!r.ok){{out.className='error';out.textContent=d.detail||'Ошибка';return}}out.className='';out.innerHTML=d.qr_url?`<p>Оплатите указанную сумму и загрузите чек.</p><img src="${{d.qr_url}}" alt="QR для оплаты" style="max-width:260px;width:100%"><input id="receipt" type="file" accept="image/png,image/jpeg,image/webp,application/pdf"><button class="button primary" onclick="uploadReceipt(${{d.payment_id}})">Отправить чек</button>`:`<p>${{d.instructions||'Оплатите по указанным реквизитам и загрузите чек.'}}</p><input id="receipt" type="file" accept="image/png,image/jpeg,image/webp,application/pdf"><button class="button primary" onclick="uploadReceipt(${{d.payment_id}})">Отправить чек</button>`}}async function uploadReceipt(id){{const file=document.getElementById('receipt').files[0];if(!file)return alert('Выберите файл чека');const data=await new Promise(ok=>{{const reader=new FileReader();reader.onload=()=>ok(reader.result.split(',')[1]);reader.readAsDataURL(file)}});const r=await fetch(`/web/payments/${{id}}/receipt`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{filename:file.name,mime_type:file.type,data_base64:data}})}});const d=await r.json();if(r.ok){{alert('Чек отправлен. Платёж ожидает проверки администратора.');location.reload()}}else alert(d.detail||'Ошибка загрузки')}};</script>'''
    await db.commit()
    return HTMLResponse(_shell(body, title="Управление подпиской — Freedom VPN"), headers=_headers())


@router.post("/cabinet/logout")
async def cabinet_logout():
    response = RedirectResponse("/", status_code=303, headers=_headers())
    response.delete_cookie(COOKIE, path="/")
    return response


@router.post("/web/payments/manual")
async def web_manual_payment(data: WebOrder, cabinet_token: str | None = Cookie(default=None, alias=COOKIE), db: AsyncSession = Depends(get_db)):
    user, _ = await _require_cabinet(cabinet_token, db)
    method = await db.scalar(select(PaymentMethod).where(PaymentMethod.code == data.method_code, PaymentMethod.is_active.is_(True)))
    if method is None:
        raise HTTPException(status_code=404, detail="Способ оплаты не найден")
    try:
        payment = await create_payment(
            db,
            PaymentCreate(user_id=user.id, plan_id=data.plan_id, node_id=data.node_id, client_type="universal", flow="", fingerprint="firefox", idempotency_key=f"web:{user.id}:{secrets.token_hex(12)}"),
            provider="manual_bank",
        )
    except PaymentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payment.details = {**(payment.details or {}), "method_code": method.code, "source": "web_cabinet"}
    await db.commit()
    return {"payment_id": payment.id, "status": payment.status, "amount": str(payment.amount), "currency": payment.currency, "instructions": method.url, "qr_url": f"/web/payment-methods/{method.id}/image" if method.image_data else None}


@router.get("/web/payment-methods/{method_id}/image")
async def web_payment_image(method_id: int, cabinet_token: str | None = Cookie(default=None, alias=COOKIE), db: AsyncSession = Depends(get_db)):
    await _require_cabinet(cabinet_token, db)
    method = await db.get(PaymentMethod, method_id)
    if method is None or method.image_data is None or not method.is_active:
        raise HTTPException(status_code=404, detail="QR не найден")
    return Response(content=method.image_data, media_type=method.image_mime_type or "image/png", headers=_headers())


@router.post("/web/payments/{payment_id}/receipt")
async def web_payment_receipt(payment_id: int, data: WebReceipt, cabinet_token: str | None = Cookie(default=None, alias=COOKIE), db: AsyncSession = Depends(get_db)):
    user, _ = await _require_cabinet(cabinet_token, db)
    payment = await db.get(Payment, payment_id)
    if payment is None or payment.user_id != user.id:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    if payment.provider != "manual_bank" or payment.status not in {"pending", "processing"}:
        raise HTTPException(status_code=409, detail="Этот платёж не принимает чек")
    try:
        receipt = base64.b64decode(data.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Некорректный файл") from exc
    if not receipt or len(receipt) > 8_000_000:
        raise HTTPException(status_code=413, detail="Размер чека должен быть от 1 байта до 8 МБ")
    payment.receipt_data = receipt
    payment.receipt_filename = data.filename
    payment.receipt_mime_type = data.mime_type
    payment.status = "processing"
    payment.details = {**(payment.details or {}), "receipt": {"source": "web_cabinet", "media_type": "document"}}
    await db.commit()
    return {"payment_id": payment.id, "status": payment.status}
