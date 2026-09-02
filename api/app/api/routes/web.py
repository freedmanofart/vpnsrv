from __future__ import annotations

import html
import hmac
import re
import secrets
import base64
import binascii
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.client import build_client_uri
from app.core.config import settings
from app.db.models.cabinet_access import CabinetAccessToken
from app.db.models.cabinet_login_code import CabinetLoginCode
from app.db.models.plan import Plan
from app.db.models.payment import Payment
from app.db.models.payment_method import PaymentMethod
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.session import get_db
from app.services.email import EmailDeliveryError, send_cabinet_code
from app.services.payments import PaymentError, create_payment
from app.schemas.payment import PaymentCreate
from app.core.security import hash_password, require_api_access, verify_password


router = APIRouter(tags=["Web cabinet"])
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
COOKIE = "freedom_cabinet"
MANUAL_PAYMENT_METHODS = {"sber_qr", "tbank_qr", "phone_transfer"}


class Registration(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    plan_id: int | None = None


class PasswordLogin(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class EmailCodeLogin(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")


class PasswordSet(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class TelegramCabinetLink(BaseModel):
    telegram_id: int
    email: str = Field(min_length=5, max_length=320)


class WebOrder(BaseModel):
    plan_id: int
    node_id: int | None = None
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


def _temporary_registration_button() -> str:
    if not settings.cabinet_allow_temporary_registration:
        return ""
    return '<form method="post" action="/web/temporary-register" class="actions"><button class="button primary" type="submit">Зарегистрироваться без email</button></form><p class="muted">Временный вход действует только в этом браузере. Добавьте email позже, чтобы не потерять доступ.</p>'


async def _plans(db: AsyncSession) -> list[Plan]:
    result = await db.execute(
        select(Plan).where(Plan.is_active.is_(True), Plan.is_public.is_(True)).order_by(Plan.price, Plan.duration_days)
    )
    return list(result.scalars())


TIER_META = {
    "lite": ("Лайт", "5 подключений", "250 ГБ трафика"),
    "standard": ("Стандарт", "15 подключений", "650 ГБ трафика"),
    "ultra": ("Ультра", "30 подключений", "3 ТБ трафика"),
}


def _tier(plan: Plan) -> str | None:
    prefix = plan.code.partition("_")[0].lower()
    if prefix in TIER_META:
        return prefix
    return next(
        (key for key, (_, connections, _) in TIER_META.items() if connections.startswith(str(plan.max_connections))),
        None,
    )


def _clean_plan_name(name: str) -> str:
    return re.sub(r"[^\w\s%()\-]+", "", name, flags=re.UNICODE).strip()


def _plan_cards(plans: list[Plan]) -> str:
    cards = []
    for plan in plans:
        tier = _tier(plan)
        if tier is None:
            continue
        title, connections, traffic_label = TIER_META[tier]
        devices = "без ограничений" if not plan.max_connections else str(plan.max_connections)
        traffic = "без ограничений" if not plan.traffic_limit_gb else f"{plan.traffic_limit_gb} ГБ"
        cards.append(
            f'<article class="plan {"featured" if tier == "standard" else ""}"><span class="pill">{title}</span>'
            f'<p class="plan-copy">{connections} · {traffic_label}</p>'
            f'<div class="price">{plan.price:g} ₽ <small>/ месяц</small></div>'
            f'<ul><li>До {devices} одновременных подключений</li><li>{traffic} трафика</li><li>Все поддерживаемые устройства</li></ul>'
            f'<button class="button primary" data-plan="{plan.id}">Выбрать</button></article>'
        )
    return "".join(cards) or '<p class="muted">Публичные тарифы временно недоступны.</p>'


def _tier_selector(plans: list[Plan]) -> str:
    groups: dict[str, list[Plan]] = {key: [] for key in TIER_META}
    for plan in plans:
        tier = _tier(plan)
        if tier:
            groups[tier].append(plan)
    cards = []
    for tier, tier_plans in groups.items():
        if not tier_plans:
            continue
        title, connections, traffic = TIER_META[tier]
        ordered_plans = sorted(
            tier_plans,
            key=lambda item: (abs(item.duration_days - 30), item.duration_days),
        )
        buttons = "".join(
            f'<button type="button" class="duration{" duration-extra" if index else ""}" data-order-plan="{plan.id}">'
            f'{html.escape(_clean_plan_name(plan.name))} · {plan.price:g} ₽</button>'
            for index, plan in enumerate(ordered_plans)
        )
        cards.append(
            f'<article class="tier-group" data-tier="{tier}"><h3>{title}</h3>'
            f'<p class="muted">{connections} · {traffic}</p>'
            f'<div class="duration-buttons">{buttons}</div></article>'
        )
    return "".join(cards)


def _shell(content: str, *, title: str = "Freedom VPN") -> str:
    return f"""<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"referrer\" content=\"no-referrer\"><title>{html.escape(title)}</title><style>
:root{{--navy:#061541;--blue:#175cff;--green:#18a870;--ink:#111827;--muted:#667085;--line:#e6eaf2;--bg:#f5f8ff;--pink:#e85faf;--purple:#a94df1}}*{{box-sizing:border-box}}body{{margin:0;font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:white}}a{{color:inherit;text-decoration:none}}.wrap{{max-width:1180px;margin:auto;padding:0 44px}}nav{{height:76px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eef1f6}}.brand{{font-size:20px;font-weight:800;letter-spacing:-.04em;display:flex;align-items:center;gap:11px}}.brand img{{width:42px;height:42px;object-fit:contain}}.brand i{{font-style:normal;color:var(--blue)}}.links{{display:flex;gap:30px;align-items:center;color:#687386}}.button{{border:1px solid var(--line);border-radius:999px;padding:12px 19px;background:#fff;font:inherit;font-weight:700;cursor:pointer;transition:.2s}}.button:hover{{transform:translateY(-1px);box-shadow:0 8px 22px #173f9d18}}.primary{{background:var(--blue);border-color:var(--blue);color:#fff}}.hero{{background:var(--navy);color:white;border-radius:0 0 30px 30px;padding:75px 0 82px;overflow:hidden;position:relative}}.hero .button:not(.primary){{color:var(--ink)}}.hero:after{{content:"";position:absolute;width:430px;height:430px;border-radius:50%;right:-150px;top:-160px;background:#12389c55}}.hero-grid{{display:grid;grid-template-columns:1.02fr .98fr;gap:42px;align-items:center;position:relative;z-index:1}}h1{{font-size:clamp(42px,5.3vw,70px);line-height:.99;letter-spacing:-.065em;margin:15px 0 23px}}.lead{{font-size:17px;color:#b9c5df;max-width:575px}}.eyebrow{{color:#54e4c0;text-transform:uppercase;letter-spacing:.19em;font-size:11px;font-weight:800}}.preview,.panel{{background:#fff;color:var(--ink);border-radius:22px;padding:24px;border:1px solid var(--line)}}.preview{{padding:14px;box-shadow:0 25px 70px #244aa218}}.preview-logo{{width:100%;max-height:300px;object-fit:cover;border-radius:18px;margin-bottom:18px}}.key{{background:#f5f7fb;border:1px solid var(--line);padding:16px;border-radius:13px;overflow:hidden}}.key code{{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}}.stat{{border:1px solid var(--line);border-radius:13px;padding:13px}}.muted{{color:var(--muted)}}section{{padding:80px 0}}h2{{font-size:42px;letter-spacing:-.055em;line-height:1.04;margin:0 0 12px}}.plans{{display:grid;grid-template-columns:repeat(3,1fr);gap:13px;margin-top:30px}}.plan{{border:1px solid var(--line);border-radius:20px;padding:23px;background:white}}.plan.featured{{border:2px solid var(--blue);padding:22px;box-shadow:0 14px 30px #175cff14}}.plan .price{{font-size:30px;font-weight:800;margin-top:20px;letter-spacing:-.05em}}.plan .price small{{font-size:12px;color:#8993a5;letter-spacing:0}}.pill{{font-weight:800;font-size:18px}}.plan-copy{{color:#8a94a4;font-size:12px}}.plan ul{{padding-left:19px;color:#536078;min-height:105px}}.plan .button{{width:100%}}.alt{{background:var(--bg)}}.features{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.feature{{background:white;border:1px solid var(--line);border-radius:18px;padding:22px}}footer{{padding:36px 0;color:var(--muted)}}.modal{{display:none;position:fixed;inset:0;background:#0c173e99;z-index:5;align-items:center;justify-content:center;padding:20px}}.modal.open{{display:flex}}.modal-card{{background:white;border-radius:20px;width:min(480px,100%);padding:25px}}input,select{{width:100%;padding:13px;border:1px solid #d9dfeb;border-radius:11px;font:inherit;margin:7px 0}}.error{{color:#c83232}}.success{{color:var(--green)}}.cabinet{{padding:48px 0 80px}}.cabinet-grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}}.panel{{box-shadow:none}}.status{{color:var(--green);font-weight:800}}.actions{{display:flex;gap:9px;flex-wrap:wrap;margin-top:18px}}.login-page{{min-height:calc(100vh - 77px);background:var(--bg);padding:48px 18px 80px}}.login-card{{max-width:620px;margin:0 auto;background:#fff;border:1px solid var(--line);border-radius:22px;padding:32px;box-shadow:none}}.login-card h1{{font-size:42px;color:var(--ink);margin:0 0 8px}}.login-card .muted{{color:var(--muted);font-size:16px}}.login-card label{{display:block;color:var(--ink);font-size:15px;font-weight:700;margin-top:26px}}.login-card input{{background:#fff;border:1px solid #d9dfeb;border-radius:11px;padding:13px;color:var(--ink);font-size:15px;margin-top:7px}}.login-tabs{{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);border-radius:999px;padding:4px;margin-top:20px;background:#f7f9fd}}.login-tabs span{{padding:10px 14px;text-align:center;color:var(--muted);font-size:15px;font-weight:700;border-radius:999px}}.login-tabs .active{{background:var(--blue);color:#fff}}.gradient{{background:var(--blue);border-color:var(--blue);color:#fff;width:100%;font-size:15px;padding:13px 19px;margin-top:14px}}.tier-groups{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:18px 0}}.tier-group{{border:1px solid var(--line);border-radius:18px;padding:18px;cursor:pointer}}.tier-group.selected{{border:2px solid var(--blue);padding:17px}}.duration-buttons{{display:flex;flex-wrap:wrap;gap:7px;margin-top:13px}}.duration{{border:1px solid var(--line);background:#fff;border-radius:999px;padding:8px 10px;cursor:pointer}}.duration-extra{{display:none}}.tier-group.expanded .duration-extra{{display:inline-block}}.duration.selected{{background:var(--blue);color:#fff;border-color:var(--blue)}}@media(max-width:760px){{.wrap{{padding:0 24px}}.links a:not(.button){{display:none}}.hero-grid,.plans,.features,.cabinet-grid,.tier-groups{{grid-template-columns:1fr}}.hero{{padding:54px 0 60px}}.stats{{grid-template-columns:1fr}}.login-card{{padding:24px}}.login-card h1{{font-size:34px}}}}
.site-top{{max-width:1180px;margin:0 auto;overflow:hidden;background:#fff}}.site-top .f-wrap{{padding:0 44px}}.f-nav{{height:76px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eef1f6;position:relative;z-index:4}}.f-brand{{display:flex;align-items:center;gap:11px;font-size:20px;font-weight:800;letter-spacing:-.04em}}.f-brand i,.f-preview-brand i{{font-style:normal;color:var(--blue)}}.f-brand .f-mark{{display:block;width:42px;height:42px;object-fit:contain}}.f-links{{display:flex;align-items:center;gap:34px;color:#687386;font-size:14px;font-weight:550}}.f-links a:hover{{color:var(--blue)}}.f-actions{{display:flex;gap:10px;align-items:center}}.f-btn{{display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line);border-radius:999px;background:#fff;color:var(--ink);font-weight:650;padding:12px 17px;cursor:pointer;transition:.2s transform,.2s box-shadow}}.f-btn:hover{{transform:translateY(-1px);box-shadow:0 8px 22px #173f9d18}}.f-btn.primary{{border-color:var(--blue);background:var(--blue);color:#fff;padding-left:21px;padding-right:21px}}.f-hero{{display:grid;grid-template-columns:minmax(0,1.02fr) minmax(360px,.98fr);gap:42px;align-items:center;padding:75px 44px 82px;background:var(--navy);border-radius:0 0 30px 30px;position:relative;overflow:hidden}}.f-hero:before{{content:"";position:absolute;width:430px;height:430px;border-radius:50%;right:-150px;top:-160px;background:#12389c;opacity:.22;filter:blur(2px)}}.f-hero>div{{position:relative;z-index:1}}.f-kicker{{display:flex;align-items:center;gap:11px;color:#54e4c0;font-size:11px;letter-spacing:.19em;text-transform:uppercase;font-weight:800;margin-bottom:22px}}.f-kicker i{{display:block;width:28px;height:2px;background:var(--blue);border-radius:99px}}.f-hero h1{{color:#fff;font-size:clamp(42px,5.3vw,70px);line-height:.99;letter-spacing:-.065em;margin:0 0 23px;font-weight:780;max-width:570px}}.f-lead{{font-size:17px;line-height:1.6;color:#b9c5df;max-width:575px;margin:0 0 30px}}.f-hero-actions{{display:flex;gap:11px;flex-wrap:wrap}}.f-trust{{display:flex;gap:20px;flex-wrap:wrap;margin-top:28px;color:#c0cae0;font-size:12px}}.f-trust span{{display:flex;align-items:center;gap:7px}}.f-dot{{width:7px;height:7px;border-radius:50%;background:var(--green)}}.f-visual{{position:relative;min-width:0;align-self:center}}.f-preview{{border:1px solid #e2e7f0;background:#f9fbff;border-radius:27px;padding:14px;box-shadow:0 25px 70px #244aa218}}.f-preview-inner{{border-radius:18px;background:#fff;border:1px solid #edf0f5;padding:24px}}.f-preview-top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px}}.f-preview-brand{{display:flex;align-items:center;gap:9px;font-weight:800}}.f-preview-brand .f-mark{{width:32px;height:32px;object-fit:contain}}.f-status{{color:#159567;background:#e6f8f0;border-radius:999px;padding:7px 10px;font-size:11px;font-weight:750;display:flex;align-items:center;gap:6px}}.f-status b{{width:6px;height:6px;background:#16a570;border-radius:50%}}.f-key{{background:#f5f7fb;border:1px solid #e7ebf2;border-radius:13px;padding:16px;margin-bottom:12px}}.f-label{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#9aa4b4;font-weight:800}}.f-code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#44506a;font-size:11px;line-height:1.6;margin-top:8px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}.f-statrow{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.f-stat{{border:1px solid #e7ebf2;border-radius:13px;padding:15px}}.f-stat .f-label{{display:block;margin-bottom:7px}}.f-stat strong{{font-size:15px}}.f-devices{{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}}.f-device{{padding:8px 11px;border:1px solid #e5eaf2;border-radius:999px;color:#697487;font-size:11px}}.plan-modal{{width:min(820px,100%)}}.plan-modal .tier-groups{{margin:18px 0 22px}}.login-tabs button{{border:0;background:transparent;padding:10px 14px;text-align:center;color:var(--muted);font:inherit;font-weight:700;border-radius:999px;cursor:pointer}}.login-tabs button.active{{background:var(--blue);color:#fff}}.login-mode[hidden]{{display:none}}.password-heading{{display:flex;justify-content:space-between;align-items:center;margin-top:22px}}.password-heading label{{margin:0}}.password-heading button{{border:0;background:transparent;color:var(--blue);font:inherit;font-weight:700;cursor:pointer}}@media(max-width:850px){{.site-top .f-wrap{{padding:0 24px}}.f-links{{display:none}}.f-hero{{grid-template-columns:1fr;padding:54px 24px 60px}}.f-preview{{max-width:580px}}}}@media(max-width:520px){{.f-actions .f-btn.primary{{padding-left:14px;padding-right:14px}}.f-actions .f-btn:not(.primary){{display:none}}.f-nav{{height:68px}}.f-preview-inner{{padding:17px}}.f-statrow{{grid-template-columns:1fr}}}}
</style></head><body>{content}</body></html>"""


@router.get("/", response_class=HTMLResponse)
async def landing(db: AsyncSession = Depends(get_db)):
    all_plans = [plan for plan in await _plans(db) if _tier(plan)]
    monthly_plans = [plan for plan in all_plans if plan.duration_days == 30]
    body = f"""<div class=\"site-top\"><div class=\"f-wrap\"><nav class=\"f-nav\" aria-label=\"Основная навигация\"><a class=\"f-brand\" href=\"/\"><img class=\"f-mark\" src=\"/static/freedom-vpn-logo-web.webp\" width=\"42\" height=\"42\" alt=\"\"><span>Freedom <i>VPN</i></span></a><div class=\"f-links\"><a href=\"#advantages\">Возможности</a><a href=\"#plans\">Тарифы</a></div><div class=\"f-actions\"><a class=\"f-btn\" href=\"/cabinet\">Кабинет</a><button class=\"f-btn primary\" type=\"button\" data-choose-plan>Подключить</button></div></nav></div><section class=\"f-hero\"><div><div class=\"f-kicker\"><i></i>Свободный интернет без лишнего</div><h1>Быстрый и приватный интернет</h1><p class=\"f-lead\">Freedom VPN защищает ваше соединение и открывает доступ к сайтам и сервисам. Один аккаунт — все устройства, без рекламы и слежки.</p><div class=\"f-hero-actions\"><button class=\"f-btn primary\" type=\"button\" data-choose-plan>Выбрать подписку ↗</button><a class=\"f-btn\" href=\"#advantages\">Как это работает</a></div><div class=\"f-trust\"><span><b class=\"f-dot\"></b>Сервера в 12 странах</span><span><b class=\"f-dot\"></b>Поддержка 24/7</span><span><b class=\"f-dot\"></b>Без логов</span></div></div><div class=\"f-visual\"><div class=\"f-preview\" aria-label=\"Предпросмотр личного кабинета\"><div class=\"f-preview-inner\"><div class=\"f-preview-top\"><div class=\"f-preview-brand\"><img class=\"f-mark\" src=\"/static/freedom-vpn-logo-web.webp\" width=\"32\" height=\"32\" alt=\""><span>Freedom <i>VPN</i></span></div><div class=\"f-status\"><b></b>Подключено</div></div><div class=\"f-key\"><div class=\"f-label\">Ключ доступа</div><div class=\"f-code\">freedom://vpn_7b91••••••••••••••••••••</div></div><div class=\"f-statrow\"><div class=\"f-stat\"><span class=\"f-label\">Локация</span><strong>Германия</strong></div><div class=\"f-stat\"><span class=\"f-label\">Задержка</span><strong>24 мс</strong></div></div><div class=\"f-devices\"><span class=\"f-device\">iOS</span><span class=\"f-device\">Android</span><span class=\"f-device\">Windows</span><span class=\"f-device\">Роутер</span></div></div></div></div></section></div>
<main><section id=\"plans\"><div class=\"wrap\"><h2>Выберите свой формат</h2><div class=\"plans\">{_plan_cards(monthly_plans)}</div></div></section><section id=\"advantages\" class=\"alt\"><div class=\"wrap features\"><article class=\"feature\"><h3>Быстро везде</h3><p class=\"muted\">Оптимальные маршруты и стабильная скорость на всех устройствах.</p></article><article class=\"feature\"><h3>Приватность по умолчанию</h3><p class=\"muted\">Шифрование трафика защищает данные в любой сети.</p></article><article class=\"feature\"><h3>Все устройства</h3><p class=\"muted\">iOS, Android, Windows, macOS и роутеры.</p></article></div></section></main><footer><div class=\"wrap\">Freedom VPN · Свобода быть собой в интернете</div></footer>
<div class=\"modal\" id=\"register\"><div class=\"modal-card plan-modal\"><h3>Выберите подписку</h3><p class=\"muted\">Нажмите на срок, чтобы раскрыть остальные планы этой группы.</p><div class=\"tier-groups\">{_tier_selector(all_plans)}</div><input id=\"plan\" type=\"hidden\"><p id=\"result\"></p><div class=\"actions\"><button class=\"button\" type=\"button\" onclick=\"closeModal()\">Отмена</button><button class=\"button primary\" type=\"button\" onclick=\"checkout()\">Оплата</button></div></div></div>
<script>const modal=document.getElementById('register'),plan=document.getElementById('plan');function openPlans(id=''){{modal.classList.add('open');if(id)selectPlan(id)}}function selectPlan(id){{plan.value=id;document.querySelectorAll('[data-order-plan]').forEach(x=>x.classList.toggle('selected',x.dataset.orderPlan===String(id)));document.querySelectorAll('.tier-group').forEach(x=>x.classList.toggle('selected',!!x.querySelector('.selected')));document.querySelector(`[data-order-plan="${{id}}"]`)?.closest('.tier-group')?.classList.add('expanded')}}function selectFirstPlanInGroup(group){{const first=group.querySelector('[data-order-plan]');if(first)selectPlan(first.dataset.orderPlan);group.classList.add('expanded')}}document.querySelectorAll('[data-choose-plan]').forEach(b=>b.onclick=()=>openPlans());document.querySelectorAll('[data-plan]').forEach(b=>b.onclick=()=>openPlans(b.dataset.plan));document.querySelectorAll('.plan-modal .tier-group').forEach(group=>group.onclick=e=>{{if(e.target.closest('[data-order-plan]'))return;selectFirstPlanInGroup(group)}});document.querySelectorAll('[data-order-plan]').forEach(b=>b.onclick=e=>{{e.stopPropagation();selectPlan(b.dataset.orderPlan)}});function closeModal(){{modal.classList.remove('open')}}function checkout(){{const out=document.getElementById('result');if(!plan.value){{out.className='error';out.textContent='Выберите подписку';return}}location.href=`/cabinet?checkout=1&plan_id=${{encodeURIComponent(plan.value)}}#payment`}}</script>"""
    return HTMLResponse(_shell(body), headers=_headers())


def _code_digest(user_id: int, code: str) -> str:
    return hmac.new(
        settings.service_api_token.encode(),
        f"cabinet-login:{user_id}:{code}".encode(),
        sha256,
    ).hexdigest()


async def _issue_code(user: User, email_address: str, db: AsyncSession) -> datetime:
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.cabinet_email_code_ttl_minutes
    )
    await db.execute(delete(CabinetLoginCode).where(CabinetLoginCode.user_id == user.id))
    db.add(
        CabinetLoginCode(
            user_id=user.id,
            code_hash=_code_digest(user.id, code),
            expires_at=expires,
        )
    )
    try:
        await send_cabinet_code(
            email_address,
            code,
            settings.cabinet_email_code_ttl_minutes,
        )
    except EmailDeliveryError as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await db.commit()
    return expires


def _set_cabinet_cookie(response: Response, raw: str) -> None:
    secure = urlparse(settings.public_base_url).scheme == "https"
    response.set_cookie(
        COOKIE,
        raw,
        max_age=settings.cabinet_token_ttl_days * 86400,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


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
        user = User(telegram_id=-secrets.randbelow(9_000_000_000_000_000) - 1, email=email_address, status="active")
        db.add(user)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            user = await db.scalar(select(User).where(User.email == email_address))
            if user is None:
                raise HTTPException(status_code=409, detail="Не удалось создать пользователя")
    expires = await _issue_code(user, email_address, db)
    return {"message": "Код для входа отправлен на почту", "expires_at": expires}


@router.post("/web/code/login")
async def email_code_login(data: EmailCodeLogin, db: AsyncSession = Depends(get_db)):
    email_address = data.email.strip().lower()
    user = await db.scalar(select(User).where(User.email == email_address))
    login_code = None
    if user is not None:
        login_code = await db.scalar(
            select(CabinetLoginCode)
            .where(CabinetLoginCode.user_id == user.id)
            .order_by(CabinetLoginCode.id.desc())
            .with_for_update()
        )

    now = datetime.now(timezone.utc)
    expires_at = None if login_code is None else login_code.expires_at.replace(
        tzinfo=login_code.expires_at.tzinfo or timezone.utc
    )
    submitted_hash = _code_digest(user.id if user is not None else 0, data.code)
    valid = (
        user is not None
        and user.status == "active"
        and login_code is not None
        and login_code.used_at is None
        and login_code.attempts < 5
        and expires_at is not None
        and expires_at > now
        and secrets.compare_digest(login_code.code_hash, submitted_hash)
    )
    if not valid:
        if login_code is not None and login_code.used_at is None:
            login_code.attempts += 1
            if login_code.attempts >= 5 or (expires_at is not None and expires_at <= now):
                login_code.used_at = now
            await db.commit()
        raise HTTPException(status_code=401, detail="Неверный или просроченный код")

    login_code.used_at = now
    raw = secrets.token_urlsafe(32)
    session_expires = now + timedelta(days=settings.cabinet_token_ttl_days)
    db.add(
        CabinetAccessToken(
            user_id=user.id,
            token_hash=_digest(raw),
            expires_at=session_expires,
        )
    )
    await db.commit()
    response = JSONResponse(
        {"message": "Вход выполнен", "next_url": "/cabinet"},
        headers=_headers(),
    )
    _set_cabinet_cookie(response, raw)
    return response


@router.post("/web/password/login")
async def password_login(data: PasswordLogin, db: AsyncSession = Depends(get_db)):
    email_address = data.email.strip().lower()
    user = await db.scalar(select(User).where(User.email == email_address))
    if user is None or user.status != "active" or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.cabinet_token_ttl_days)
    db.add(CabinetAccessToken(user_id=user.id, token_hash=_digest(raw), expires_at=expires))
    await db.commit()
    response = JSONResponse({"message": "Вход выполнен"}, headers=_headers())
    _set_cabinet_cookie(response, raw)
    return response


@router.post("/web/telegram-cabinet-link", dependencies=[Depends(require_api_access)])
async def telegram_cabinet_link(data: TelegramCabinetLink, db: AsyncSession = Depends(get_db)):
    email_address = data.email.strip().lower()
    if not EMAIL_RE.fullmatch(email_address):
        raise HTTPException(status_code=422, detail="Укажите корректный email")
    user = await db.scalar(select(User).where(User.telegram_id == data.telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь Telegram не найден")
    owner = await db.scalar(select(User).where(User.email == email_address, User.id != user.id))
    if owner is not None:
        raise HTTPException(status_code=409, detail="Этот email уже связан с другим аккаунтом")
    user.email = email_address
    expires = await _issue_code(user, email_address, db)
    return {"message": "Код для входа отправлен на почту", "expires_at": expires}


@router.post("/web/temporary-register")
async def temporary_register(db: AsyncSession = Depends(get_db)):
    if not settings.cabinet_allow_temporary_registration:
        raise HTTPException(status_code=404, detail="Временная регистрация отключена")
    user = User(
        telegram_id=-secrets.randbelow(9_000_000_000_000_000) - 1,
        status="active",
    )
    db.add(user)
    await db.flush()
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.cabinet_token_ttl_days)
    db.add(CabinetAccessToken(user_id=user.id, token_hash=_digest(raw), expires_at=expires))
    await db.commit()
    response = RedirectResponse("/cabinet", status_code=303, headers=_headers())
    secure = urlparse(settings.public_base_url).scheme == "https"
    response.set_cookie(
        COOKIE,
        raw,
        max_age=settings.cabinet_token_ttl_days * 86400,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    return response


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


@router.post("/web/password")
async def set_password(
    data: PasswordSet,
    cabinet_token: str | None = Cookie(default=None, alias=COOKIE),
    db: AsyncSession = Depends(get_db),
):
    user, _ = await _require_cabinet(cabinet_token, db)
    user.password_hash = hash_password(data.password)
    await db.commit()
    return {"message": "Пароль сохранён"}


@router.get("/cabinet/password", response_class=HTMLResponse)
async def password_setup_page(
    cabinet_token: str | None = Cookie(default=None, alias=COOKIE),
    db: AsyncSession = Depends(get_db),
):
    await _require_cabinet(cabinet_token, db)
    body = '''<div class="wrap"><nav><a class="brand" href="/"><img src="/static/freedom-vpn-logo-web.webp" alt="">Freedom <i>VPN</i></a></nav><main class="login-page"><section class="login-card"><h1>Создайте пароль</h1><p class="muted">После этого вы сможете выбирать вход по письму или паролю.</p><label for="new-password">Пароль</label><input id="new-password" type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="Не менее 8 символов"><button class="button gradient" type="button" onclick="savePassword()">Сохранить и открыть кабинет</button><p id="password-result"></p><p class="login-footer"><a href="/cabinet">Настроить позже</a></p></section></main><script>async function savePassword(){const out=document.getElementById('password-result');out.textContent='Сохраняем…';const r=await fetch('/web/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('new-password').value})});const d=await r.json();if(r.ok){location.href='/cabinet';return}out.className='error';out.textContent=d.detail||'Не удалось сохранить пароль'}</script>'''
    return HTMLResponse(_shell(body, title="Создание пароля — Freedom VPN"), headers=_headers())


@router.get("/cabinet/access/{token}")
async def cabinet_access(token: str, db: AsyncSession = Depends(get_db)):
    found = await _access(token, db)
    if found is None:
        return HTMLResponse(_shell('<main class="wrap cabinet"><div class="panel"><h2>Ссылка недействительна</h2><p class="muted">Запросите новую ссылку на главной странице.</p><a class="button primary" href="/">На главную</a></div></main>'), status_code=401, headers=_headers())
    user, access = found
    access.last_used_at = datetime.now(timezone.utc)
    await db.commit()
    response = RedirectResponse(
        "/cabinet/password" if not user.password_hash else "/cabinet",
        status_code=303,
        headers=_headers(),
    )
    _set_cabinet_cookie(response, token)
    return response


@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(
    plan_id: int | None = None,
    checkout: bool = False,
    cabinet_token: str | None = Cookie(default=None, alias=COOKIE),
    db: AsyncSession = Depends(get_db),
):
    found = await _access(cabinet_token, db)
    if found is None:
        body = f'''<div class="wrap"><nav><a class="brand" href="/"><img src="/static/freedom-vpn-logo-web.webp" alt="">Freedom <i>VPN</i></a><a class="button" href="/">На главную</a></nav></div><main class="login-page"><section class="login-card"><h1>Вход в аккаунт</h1><p class="muted">Рады видеть вас снова.</p><label for="login-email">Email</label><input id="login-email" type="email" autocomplete="email" placeholder="you@example.com"><div class="login-tabs"><button class="active" type="button" data-login-mode="email">Код из письма</button><button type="button" data-login-mode="password">Пароль</button></div><div class="login-mode" data-mode-panel="email"><p class="muted" style="margin-top:22px">Пришлём шестизначный одноразовый код — пароль не нужен.</p><button class="button gradient" type="button" onclick="requestLogin()">Получить код на email</button><label for="login-code">Код из письма</label><input id="login-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{{6}}" placeholder="000000"><button class="button gradient" type="button" onclick="codeLogin()">Войти по коду</button></div><div class="login-mode" data-mode-panel="password" hidden><div class="password-heading"><label for="login-password">Пароль</label><button type="button" onclick="requestPasswordReset()">Получить код</button></div><input id="login-password" type="password" autocomplete="current-password" minlength="8" placeholder="••••••••"><button class="button gradient" type="button" onclick="passwordLogin()">Войти</button></div><p id="login-result"></p>{_temporary_registration_button()}</section></main><script>const tabs=document.querySelectorAll('[data-login-mode]');function selectLoginMode(mode){{tabs.forEach(x=>x.classList.toggle('active',x.dataset.loginMode===mode));document.querySelectorAll('[data-mode-panel]').forEach(panel=>panel.hidden=panel.dataset.modePanel!==mode)}}tabs.forEach(tab=>tab.onclick=()=>selectLoginMode(tab.dataset.loginMode));async function requestLogin(){{const out=document.getElementById('login-result');out.className='';out.textContent='Отправляем код…';const r=await fetch('/web/register',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:document.getElementById('login-email').value}})}});const d=await r.json();out.className=r.ok?'success':'error';out.textContent=r.ok?d.message:(d.detail||'Ошибка отправки');if(r.ok)document.getElementById('login-code').focus()}}async function requestPasswordReset(){{selectLoginMode('email');await requestLogin()}}async function codeLogin(){{const out=document.getElementById('login-result');out.className='';out.textContent='Проверяем код…';const r=await fetch('/web/code/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:document.getElementById('login-email').value,code:document.getElementById('login-code').value}})}});const d=await r.json();if(r.ok){{location.reload();return}}out.className='error';out.textContent=d.detail||'Ошибка входа'}}async function passwordLogin(){{const out=document.getElementById('login-result');out.className='';out.textContent='Проверяем…';const r=await fetch('/web/password/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:document.getElementById('login-email').value,password:document.getElementById('login-password').value}})}});const d=await r.json();if(r.ok){{location.reload();return}}out.className='error';out.textContent=d.detail||'Ошибка входа'}};</script>'''
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
    methods = list((await db.execute(select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).order_by(PaymentMethod.sort_order))).scalars())
    payments = list((await db.execute(select(Payment).where(Payment.user_id == user.id).order_by(Payment.id.desc()).limit(10))).scalars())
    tier_selector = _tier_selector(public_plans)
    requested_plan = next((item for item in public_plans if item.id == plan_id), None)
    initial_plan_id = requested_plan.id if requested_plan else (public_plans[0].id if public_plans else 0)
    method_options = "".join(
        f'<option value="{html.escape(m.code)}" data-url="{html.escape(m.url or "")}">{html.escape(m.name)}</option>'
        for m in methods
    )
    payment_rows = "".join(f'<li>Платёж #{p.id}: {p.amount:g} {html.escape(p.currency)} — <b>{html.escape(p.status)}</b></li>' for p in payments) or "<li>Платежей ещё нет</li>"
    key_block = '<p class="muted">Ключ появится после подтверждения оплаты и выдачи подписки.</p>'
    if vpn_uri:
        key_block = (
            f'<div class="key"><code id="vpn-key">{html.escape(vpn_uri)}</code></div>'
            '<div class="actions"><button class="button" onclick="copyKey(this)">Копировать ключ</button></div>'
        )
    body = f'''<div class="wrap"><nav><a class="brand" href="/"><img src="/static/freedom-vpn-logo-web.webp" alt="">Freedom <i>VPN</i></a><div class="links"><form method="post" action="/cabinet/logout"><button class="button">Выйти</button></form></div></nav><main class="cabinet"><div class="eyebrow">Управление подпиской</div><h2>{html.escape(user.email or "Ваш кабинет")}</h2><div class="cabinet-grid"><section class="panel"><h3>Подписка <span class="status">{'активна' if active else 'не активна'}</span></h3><p><b>{html.escape(_clean_plan_name(plan.name)) if plan else 'Тариф не выбран'}</b></p><div class="stats"><div class="stat"><small class="muted">Осталось</small><br><b>{days} дн.</b></div><div class="stat"><small class="muted">Трафик</small><br><b>{traffic}</b></div><div class="stat"><small class="muted">Подключения</small><br><b>{devices}</b></div></div></section><section class="panel"><h3>Ключ доступа</h3>{key_block}<p class="muted">Сервер назначается автоматически: {html.escape(node.region or node.name) if node else 'после оплаты'}</p></section></div><section><h2>Приобрести или продлить</h2><div class="tier-groups">{tier_selector}</div><input type="hidden" id="order-plan" value="{initial_plan_id}"><div class="cabinet-grid"><div class="panel"><label>Способ оплаты<select id="order-method">{method_options}</select></label><button class="button primary" onclick="createOrder()">Оплата</button><div id="order-result"></div></div><div class="panel"><h3>Последние платежи</h3><ul>{payment_rows}</ul></div></div></section><section><h2>Приложения</h2><div class="actions"><a class="button" href="https://github.com/amnezia-vpn/amnezia-client/releases/download/4.8.10.0/AmneziaVPN_4.8.10.0_windows_x64.exe">Windows</a><a class="button" href="https://github.com/amnezia-vpn/amnezia-client/releases/download/4.8.10.0/AmneziaVPN_4.8.10.0_macos.zip">macOS</a><a class="button" href="https://play.google.com/store/apps/details?id=org.amnezia.vpn">Android</a><a class="button" href="https://apps.apple.com/ru/app/defaultvpn/id6744725017">iOS</a></div></section></main></div><script>function copyKey(button){{navigator.clipboard.writeText(document.getElementById('vpn-key').textContent);button.textContent='Скопировано ✓'}}async function uploadReceipt(id){{const file=document.getElementById('receipt').files[0];if(!file)return alert('Выберите файл чека');const data=await new Promise(ok=>{{const reader=new FileReader();reader.onload=()=>ok(reader.result.split(',')[1]);reader.readAsDataURL(file)}});const r=await fetch(`/web/payments/${{id}}/receipt`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{filename:file.name,mime_type:file.type,data_base64:data}})}});const d=await r.json();if(r.ok){{alert('Чек отправлен. Платёж ожидает проверки администратора.');location.reload()}}else alert(d.detail||'Ошибка загрузки')}};</script>'''
    body = body.replace(
        "<section><h2>Приобрести или продлить</h2>",
        '<section id="payment"><h2>Приобрести или продлить</h2>',
    )
    body += f'''<script>
const initialPlanId='{initial_plan_id}';
function selectOrderPlan(button){{
  document.querySelectorAll('[data-order-plan]').forEach(item=>item.classList.remove('selected'));
  document.querySelectorAll('.tier-group').forEach(item=>item.classList.remove('selected'));
  button.classList.add('selected');
  button.closest('.tier-group').classList.add('selected','expanded');
  document.getElementById('order-plan').value=button.dataset.orderPlan;
}}
document.querySelectorAll('.cabinet .tier-group').forEach(group=>group.onclick=e=>{{
  if(e.target.closest('[data-order-plan]'))return;
  const first=group.querySelector('[data-order-plan]');
  if(first)selectOrderPlan(first);
  group.classList.add('expanded');
}});
document.querySelectorAll('[data-order-plan]').forEach(button=>button.onclick=e=>{{e.stopPropagation();selectOrderPlan(button)}});
const initialPlanButton=document.querySelector(`[data-order-plan="${{initialPlanId}}"]`);
if(initialPlanButton)selectOrderPlan(initialPlanButton);
window.createOrder=async function(){{
  const out=document.getElementById('order-result');
  const methodSelect=document.getElementById('order-method');
  const methodCode=methodSelect.value;
  const paymentUrl=methodSelect.selectedOptions[0]?.dataset.url||'';
  if(methodCode==='payment_safety'){{out.className='';out.textContent='Платёжная страница не получает ваш VPN-ключ. Доступ выдаётся только после подтверждения платежа VPN API.';return}}
  if(methodCode==='telegram_stars'){{out.className='error';out.textContent='Цена в Telegram Stars ещё не настроена для этого тарифа.';return}}
  if(!['sber_qr','tbank_qr','phone_transfer'].includes(methodCode)){{
    if(paymentUrl){{location.href=paymentUrl;return}}
    out.className='error';out.textContent='Для этого способа оплаты не настроена ссылка.';return
  }}
  out.className='';out.textContent='Создаём платёж…';
  const r=await fetch('/web/payments/manual',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{plan_id:Number(document.getElementById('order-plan').value),method_code:methodCode}})}});
  const d=await r.json();
  if(!r.ok){{out.className='error';out.textContent=d.detail||'Ошибка';return}}
  out.className='';
  out.innerHTML=d.qr_url?`<p>Оплатите указанную сумму и загрузите чек.</p><img src="${{d.qr_url}}" alt="QR для оплаты" style="max-width:260px;width:100%"><input id="receipt" type="file" accept="image/png,image/jpeg,image/webp,application/pdf"><button class="button primary" onclick="uploadReceipt(${{d.payment_id}})">Отправить чек</button>`:`<p>${{d.instructions||'Оплатите по указанным реквизитам и загрузите чек.'}}</p><input id="receipt" type="file" accept="image/png,image/jpeg,image/webp,application/pdf"><button class="button primary" onclick="uploadReceipt(${{d.payment_id}})">Отправить чек</button>`;
}};
</script>'''
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
    if method.code not in MANUAL_PAYMENT_METHODS:
        raise HTTPException(status_code=400, detail="Этот способ оплаты открывается отдельной платёжной страницей")
    node_id = data.node_id
    if node_id is None:
        active_client = await db.scalar(
            select(VPNClient)
            .where(VPNClient.user_id == user.id, VPNClient.status == "active")
            .order_by(VPNClient.id.desc())
        )
        node_id = active_client.node_id if active_client else None
    node = await db.get(VPNNode, node_id) if node_id is not None else None
    if node is None or node.status != "active":
        node = await db.scalar(
            select(VPNNode).where(VPNNode.status == "active").order_by(VPNNode.id)
        )
    if node is None:
        raise HTTPException(status_code=409, detail="Сейчас нет доступного VPN-сервера")
    try:
        payment = await create_payment(
            db,
            PaymentCreate(user_id=user.id, plan_id=data.plan_id, node_id=node.id, client_type="universal", flow="", fingerprint="firefox", idempotency_key=f"web:{user.id}:{secrets.token_hex(12)}"),
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
