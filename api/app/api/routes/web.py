from __future__ import annotations

import html
import hmac
import logging
import re
import secrets
import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.client import build_client_uri
from app.core.config import settings
from app.core.cabinet_links import telegram_cabinet_link_token, verify_telegram_cabinet_link_token
from app.db.models.cabinet_access import CabinetAccessToken
from app.db.models.cabinet_login_code import CabinetLoginCode
from app.db.models.audit import AccessGrant, ActivationCode, ClientDevice
from app.db.models.plan import Plan
from app.db.models.plan_package import PlanPackage
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
from app.services.platega import PlategaError, create_platega_payment, is_platega_method
from app.services.notifications import notify_payment_created, notify_payment_receipt
from app.schemas.payment import PaymentCreate
from app.core.security import hash_password, require_api_access, verify_password
from app.services.audit import write_audit
from app.services.threexui import ThreeXUIClient, ThreeXUIError


router = APIRouter(tags=["Web cabinet"])
logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
COOKIE = "freedom_cabinet"
MANUAL_PAYMENT_METHODS = {"sber_qr", "tbank_qr", "phone_transfer"}
PAYMENT_RETURN_TOKEN_TTL_SECONDS = 60 * 60


def _normalize_email(value: str) -> str:
    cleaned = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", value or "")
    return cleaned.strip("<>.,;:()[]{}\"'«»").lower()


class Registration(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    plan_id: int | None = None
    telegram_link_token: str | None = None


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
        select(Plan).where(Plan.is_active.is_(True), Plan.is_public.is_(True)).order_by(Plan.package_id, Plan.duration_days, Plan.price)
    )
    return list(result.scalars())


async def _plan_packages(db: AsyncSession) -> list[PlanPackage]:
    result = await db.execute(
        select(PlanPackage).where(PlanPackage.is_active.is_(True)).order_by(PlanPackage.sort_order, PlanPackage.id)
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


def _clean_payment_method_name(name: str) -> str:
    return re.sub(r"^[^\wА-Яа-яЁё]+", "", name, flags=re.UNICODE).strip()


def _masked_email(value: str | None) -> str:
    if not value or "@" not in value:
        return value or "Ваш аккаунт"
    local, domain = value.rsplit("@", 1)
    visible = local[:1] or "•"
    return f"{visible}•••@{domain}"


COUNTRY_NAMES = {
    "CA": "Канада",
    "CH": "Швейцария",
    "DE": "Германия",
    "FI": "Финляндия",
    "FR": "Франция",
    "GB": "Великобритания",
    "NL": "Нидерланды",
    "PL": "Польша",
    "RU": "Россия",
    "SE": "Швеция",
    "US": "США",
}


def _server_label(node: VPNNode | None) -> tuple[str, str]:
    if node is None:
        return "🌐", "назначится после оплаты"
    raw = (node.region or node.name or "Сервер").strip()
    parts = [part.strip() for part in raw.split("|", 1)]
    code = parts[0].upper() if len(parts[0]) == 2 and parts[0].isalpha() else ""
    label = parts[1] if len(parts) > 1 and parts[1] else COUNTRY_NAMES.get(code, raw)
    flag = "".join(chr(127397 + ord(char)) for char in code) if code else "🌐"
    return flag, label


def _cabinet_plan_name(plan: Plan | None, packages_by_id: dict[int, PlanPackage]) -> str:
    if plan is None:
        return "Тариф не выбран"
    name = _clean_plan_name(plan.name)
    package = _package_label(plan, packages_by_id)
    if package.casefold() in name.casefold():
        return name
    return f"{package} · {name}"


PAYMENT_STATUS_META = {
    "paid": ("Оплачен", "paid"),
    "pending": ("Ожидает", "pending"),
    "processing": ("В обработке", "pending"),
    "failed": ("Ошибка", "failed"),
    "cancelled": ("Отменён", "cancelled"),
    "refunded": ("Возвращён", "cancelled"),
}


def _format_money(value: Decimal, currency: str) -> str:
    amount = format(value, "f")
    if "." in amount:
        amount = amount.rstrip("0").rstrip(".")
    symbol = "₽" if currency == "RUB" else currency
    return f"{amount} {symbol}"


def _cabinet_payment_rows(payments: list[Payment]) -> str:
    if not payments:
        return '<div class="payment-empty muted">Платежей ещё нет</div>'
    rows = []
    for payment in payments[:5]:
        label, css_class = PAYMENT_STATUS_META.get(payment.status, (payment.status, "pending"))
        created_at = payment.created_at
        date_label = created_at.strftime("%d.%m.%Y") if created_at else "—"
        rows.append(
            '<div class="payment-row">'
            f'<b>#{payment.id}</b>'
            f'<span>{html.escape(_format_money(payment.amount, payment.currency))}</span>'
            f'<span class="payment-state {css_class}">{html.escape(label)}</span>'
            f'<time>{date_label}</time>'
            '</div>'
        )
    return "".join(rows)


def _format_gb(value: float) -> str:
    if value <= 0:
        return "0 ГБ"
    if float(value).is_integer():
        return f"{value:.0f} ГБ"
    return f"{value:.1f} ГБ"


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


def _package_code(plan: Plan, packages_by_id: dict[int, PlanPackage] | None = None) -> str | None:
    if packages_by_id and plan.package_id and plan.package_id in packages_by_id:
        return packages_by_id[plan.package_id].code
    return _tier(plan)


def _package_label(plan: Plan, packages_by_id: dict[int, PlanPackage] | None = None) -> str:
    if packages_by_id and plan.package_id and plan.package_id in packages_by_id:
        return packages_by_id[plan.package_id].name
    tier = _tier(plan)
    return TIER_META[tier][0] if tier else "Тариф"


def _tier_selector(plans: list[Plan], packages: list[PlanPackage] | None = None) -> str:
    packages_by_id = {item.id: item for item in packages or []}
    groups: dict[str, list[Plan]] = {key: [] for key in TIER_META}
    package_meta: dict[str, tuple[str, str, str]] = dict(TIER_META)
    for package in packages or []:
        package_meta[package.code] = (
            package.name,
            "без ограничений" if not package.max_connections else f"{package.max_connections} подключений",
            "без ограничений" if not package.traffic_limit_gb else f"{package.traffic_limit_gb} ГБ трафика",
        )
        groups.setdefault(package.code, [])
    for plan in plans:
        tier = _package_code(plan, packages_by_id)
        if tier:
            groups[tier].append(plan)
    cards = []
    for tier, tier_plans in groups.items():
        if not tier_plans:
            continue
        title, connections, traffic = package_meta[tier]
        ordered_plans = sorted(
            tier_plans,
            key=lambda item: (abs(item.duration_days - 30), item.duration_days),
        )
        buttons = "".join(
            f'<button type="button" class="duration{" duration-extra" if index else ""}" data-order-plan="{plan.id}">'
            f'{html.escape(_clean_plan_name(plan.name))} · {html.escape(_format_money(plan.price, plan.currency))}</button>'
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
:root{{--navy:#061541;--blue:#175cff;--green:#18a870;--ink:#111827;--muted:#667085;--line:#e6eaf2;--bg:#f5f8ff;--pink:#e85faf;--purple:#a94df1}}*{{box-sizing:border-box}}body{{margin:0;font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:white}}a{{color:inherit;text-decoration:none}}.wrap{{max-width:1180px;margin:auto;padding:0 44px}}nav{{height:76px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eef1f6}}.brand{{font-size:20px;font-weight:800;letter-spacing:-.04em;display:flex;align-items:center;gap:11px}}.brand img{{width:50px;height:50px;object-fit:contain}}.brand i{{font-style:normal;color:var(--blue)}}.links{{display:flex;gap:30px;align-items:center;color:#687386}}.button{{border:1px solid var(--line);border-radius:999px;padding:12px 19px;background:#fff;font:inherit;font-weight:700;cursor:pointer;transition:.2s}}.button:hover{{transform:translateY(-1px);box-shadow:0 8px 22px #173f9d18}}.primary{{background:var(--blue);border-color:var(--blue);color:#fff}}.hero{{background:var(--navy);color:white;border-radius:0 0 30px 30px;padding:75px 0 82px;overflow:hidden;position:relative}}.hero .button:not(.primary){{color:var(--ink)}}.hero:after{{content:"";position:absolute;width:430px;height:430px;border-radius:50%;right:-150px;top:-160px;background:#12389c55}}.hero-grid{{display:grid;grid-template-columns:1.02fr .98fr;gap:42px;align-items:center;position:relative;z-index:1}}h1{{font-size:clamp(42px,5.3vw,70px);line-height:.99;letter-spacing:-.065em;margin:15px 0 23px}}.lead{{font-size:17px;color:#b9c5df;max-width:575px}}.eyebrow{{color:#54e4c0;text-transform:uppercase;letter-spacing:.19em;font-size:11px;font-weight:800}}.preview,.panel{{background:#fff;color:var(--ink);border-radius:22px;padding:24px;border:1px solid var(--line)}}.preview{{padding:14px;box-shadow:0 25px 70px #244aa218}}.preview-logo{{width:100%;max-height:300px;object-fit:cover;border-radius:18px;margin-bottom:18px}}.key{{background:#f5f7fb;border:1px solid var(--line);padding:16px;border-radius:13px;overflow:hidden}}.key code{{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}}.stat{{border:1px solid var(--line);border-radius:13px;padding:13px}}.muted{{color:var(--muted)}}section{{padding:80px 0}}h2{{font-size:42px;letter-spacing:-.055em;line-height:1.04;margin:0 0 12px}}.plans{{display:grid;grid-template-columns:repeat(3,1fr);gap:13px;margin-top:30px}}.plan{{border:1px solid var(--line);border-radius:20px;padding:23px;background:white}}.plan.featured{{border:2px solid var(--blue);padding:22px;box-shadow:0 14px 30px #175cff14}}.plan .price{{font-size:30px;font-weight:800;margin-top:20px;letter-spacing:-.05em}}.plan .price small{{font-size:12px;color:#8993a5;letter-spacing:0}}.pill{{font-weight:800;font-size:18px}}.plan-copy{{color:#8a94a4;font-size:12px}}.plan ul{{padding-left:19px;color:#536078;min-height:105px}}.plan .button{{width:100%}}.alt{{background:var(--bg)}}.features{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.feature{{background:white;border:1px solid var(--line);border-radius:18px;padding:22px}}footer{{padding:36px 0;color:var(--muted)}}.modal{{display:none;position:fixed;inset:0;background:#0c173e99;z-index:5;align-items:center;justify-content:center;padding:20px}}.modal.open{{display:flex}}.modal-card{{background:white;border-radius:20px;width:min(480px,100%);padding:25px}}input,select{{width:100%;padding:13px;border:1px solid #d9dfeb;border-radius:11px;font:inherit;margin:7px 0}}.error{{color:#c83232}}.success{{color:var(--green)}}.cabinet{{padding:48px 0 80px}}.cabinet-grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}}.panel{{box-shadow:none}}.status{{color:var(--green);font-weight:800}}.status.inactive{{color:#dc2626}}.actions{{display:flex;gap:9px;flex-wrap:wrap;margin-top:18px}}.login-page{{min-height:calc(100vh - 77px);background:var(--bg);padding:48px 18px 80px}}.login-card{{max-width:620px;margin:0 auto;background:#fff;border:1px solid var(--line);border-radius:22px;padding:32px;box-shadow:none}}.login-card h1{{font-size:42px;color:var(--ink);margin:0 0 8px}}.login-card .muted{{color:var(--muted);font-size:16px}}.login-card label{{display:block;color:var(--ink);font-size:15px;font-weight:700;margin-top:26px}}.login-card input{{background:#fff;border:1px solid #d9dfeb;border-radius:11px;padding:13px;color:var(--ink);font-size:15px;margin-top:7px}}.login-tabs{{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);border-radius:999px;padding:4px;margin-top:20px;background:#f7f9fd}}.login-tabs span{{padding:10px 14px;text-align:center;color:var(--muted);font-size:15px;font-weight:700;border-radius:999px}}.login-tabs .active{{background:var(--blue);color:#fff}}.gradient{{background:var(--blue);border-color:var(--blue);color:#fff;width:100%;font-size:15px;padding:13px 19px;margin-top:14px}}.tier-groups{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:18px 0}}.tier-group{{border:1px solid var(--line);border-radius:18px;padding:18px}}.tier-group.selected{{border:2px solid var(--blue);padding:17px}}.duration-buttons{{display:flex;flex-wrap:wrap;gap:7px;margin-top:13px}}.duration{{border:1px solid var(--line);background:#fff;border-radius:999px;padding:8px 10px;cursor:pointer}}.duration-extra{{display:none}}.tier-group.expanded .duration-extra{{display:inline-block}}.duration.selected{{background:var(--blue);color:#fff;border-color:var(--blue)}}@media(max-width:760px){{.wrap{{padding:0 24px}}.links a:not(.button){{display:none}}.hero-grid,.plans,.features,.cabinet-grid,.tier-groups{{grid-template-columns:1fr}}.hero{{padding:54px 0 60px}}.stats{{grid-template-columns:1fr}}.login-card{{padding:24px}}.login-card h1{{font-size:34px}}}}
.site-top{{max-width:1180px;margin:0 auto;overflow:hidden;background:#fff}}.site-top .f-wrap{{padding:0 44px}}.f-nav{{height:76px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eef1f6;position:relative;z-index:4}}.f-brand{{display:flex;align-items:center;gap:11px;font-size:20px;font-weight:800;letter-spacing:-.04em}}.f-brand i,.f-preview-brand i{{font-style:normal;color:var(--blue)}}.f-brand .f-mark{{display:block;width:50px;height:50px;object-fit:contain}}.f-links{{display:flex;align-items:center;gap:34px;color:#687386;font-size:14px;font-weight:550}}.f-links a:hover{{color:var(--blue)}}.f-actions{{display:flex;gap:10px;align-items:center}}.f-btn{{display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line);border-radius:999px;background:#fff;color:var(--ink);font-weight:650;padding:12px 17px;cursor:pointer;transition:.2s transform,.2s box-shadow}}.f-btn:hover{{transform:translateY(-1px);box-shadow:0 8px 22px #173f9d18}}.f-btn.primary{{border-color:var(--blue);background:var(--blue);color:#fff;padding-left:21px;padding-right:21px}}.f-hero{{display:grid;grid-template-columns:minmax(0,1.02fr) minmax(360px,.98fr);gap:42px;align-items:center;padding:75px 44px 82px;background:var(--navy);border-radius:0 0 30px 30px;position:relative;overflow:hidden}}.f-hero:before{{content:"";position:absolute;width:430px;height:430px;border-radius:50%;right:-150px;top:-160px;background:#12389c;opacity:.22;filter:blur(2px)}}.f-hero>div{{position:relative;z-index:1}}.f-kicker{{display:flex;align-items:center;gap:11px;color:#54e4c0;font-size:11px;letter-spacing:.19em;text-transform:uppercase;font-weight:800;margin-bottom:22px}}.f-kicker i{{display:block;width:28px;height:2px;background:var(--blue);border-radius:99px}}.f-hero h1{{color:#fff;font-size:clamp(42px,5.3vw,70px);line-height:.99;letter-spacing:-.065em;margin:0 0 23px;font-weight:780;max-width:570px}}.f-lead{{font-size:17px;line-height:1.6;color:#b9c5df;max-width:575px;margin:0 0 30px}}.f-hero-actions{{display:flex;gap:11px;flex-wrap:wrap}}.f-trust{{display:flex;gap:20px;flex-wrap:wrap;margin-top:28px;color:#c0cae0;font-size:12px}}.f-trust span{{display:flex;align-items:center;gap:7px}}.f-dot{{width:7px;height:7px;border-radius:50%;background:var(--green)}}.f-visual{{position:relative;min-width:0;align-self:center}}.f-preview{{border:1px solid #e2e7f0;background:#f9fbff;border-radius:27px;padding:14px;box-shadow:0 25px 70px #244aa218}}.f-preview-inner{{border-radius:18px;background:#fff;border:1px solid #edf0f5;padding:24px}}.f-preview-top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px}}.f-preview-brand{{display:flex;align-items:center;gap:9px;font-weight:800}}.f-preview-brand .f-mark{{width:38px;height:38px;object-fit:contain}}.f-status{{color:#159567;background:#e6f8f0;border-radius:999px;padding:7px 10px;font-size:11px;font-weight:750;display:flex;align-items:center;gap:6px}}.f-status b{{width:6px;height:6px;background:#16a570;border-radius:50%}}.f-key{{background:#f5f7fb;border:1px solid #e7ebf2;border-radius:13px;padding:16px;margin-bottom:12px}}.f-label{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#9aa4b4;font-weight:800}}.f-code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#44506a;font-size:11px;line-height:1.6;margin-top:8px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}.f-statrow{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.f-stat{{border:1px solid #e7ebf2;border-radius:13px;padding:15px}}.f-stat .f-label{{display:block;margin-bottom:7px}}.f-stat strong{{font-size:15px}}.f-devices{{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}}.f-device{{padding:8px 11px;border:1px solid #e5eaf2;border-radius:999px;color:#697487;font-size:11px}}.modal{{overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;align-items:flex-start}}.modal.open{{display:block}}.modal-card{{margin:auto}}.plan-modal{{width:min(820px,100%);max-height:calc(100dvh - 40px);overflow-y:auto;-webkit-overflow-scrolling:touch}}.plan-modal .tier-groups{{margin:18px 0 22px}}.login-tabs button{{border:0;background:transparent;padding:10px 14px;text-align:center;color:var(--muted);font:inherit;font-weight:700;border-radius:999px;cursor:pointer}}.login-tabs button.active{{background:var(--blue);color:#fff}}.login-mode[hidden]{{display:none}}.password-heading{{display:flex;justify-content:space-between;align-items:center;margin-top:22px}}.password-heading label{{margin:0}}.password-heading button{{border:0;background:transparent;color:var(--blue);font:inherit;font-weight:700;cursor:pointer}}.panel,.cabinet-grid>*{{min-width:0}}.cabinet-grid{{grid-template-columns:minmax(0,1.2fr) minmax(0,.8fr)}}.key code{{white-space:normal;overflow-wrap:anywhere;word-break:break-word}}select{{appearance:none;background:#fff}}.tier-group{{cursor:pointer}}.cabinet-apps{{margin-top:34px;padding-top:26px;border-top:1px solid var(--line)}}.cabinet-apps h2{{font-size:30px;margin-bottom:12px}}.cabinet-apps .actions{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}.cabinet-apps .button{{border-color:#b7ead7;background:#eefcf6;color:var(--green);border-radius:12px;box-shadow:0 10px 24px #18a87014;text-align:center;padding:14px 16px}}.cabinet-apps .button:hover{{border-color:var(--green)}}.cabinet-purchase{{padding-top:34px}}@media(max-width:900px){{.cabinet{{padding-top:32px}}.cabinet-grid,.stats{{grid-template-columns:1fr}}.stat{{min-height:0}}.key code{{font-size:13px;line-height:1.45}}.cabinet-apps .actions{{grid-template-columns:1fr;align-items:stretch}}.cabinet-apps .button{{width:100%;text-align:center}}}}@media(max-width:850px){{.site-top .f-wrap{{padding:0 24px}}.f-links{{display:none}}.f-hero{{grid-template-columns:1fr;padding:54px 24px 60px}}.f-preview{{max-width:580px}}}}@media(max-width:520px){{.f-actions .f-btn.primary{{padding-left:14px;padding-right:14px}}.f-actions .f-btn:not(.primary){{display:none}}.f-nav{{height:68px}}.f-preview-inner{{padding:17px}}.f-statrow{{grid-template-columns:1fr}}.modal{{padding:14px}}.plan-modal{{max-height:calc(100dvh - 28px)}}}}
main.cabinet>.eyebrow,main.cabinet>h2,main.cabinet .panel,main.cabinet .cabinet-apps,main.cabinet .cabinet-purchase,main.cabinet .payment-summary h3{{text-align:center}}main.cabinet>h2{{font-size:31.5px;overflow-wrap:anywhere;word-break:break-word;margin-bottom:26px}}main.cabinet .panel>.muted,main.cabinet .panel p{{overflow-wrap:anywhere;word-break:break-word}}main.cabinet .actions{{justify-content:center}}main.cabinet .stats{{text-align:center}}main.cabinet .summary-row span{{text-align:left}}main.cabinet .summary-row b{{text-align:right;overflow-wrap:anywhere;word-break:break-word}}.f-nav,.brand{{gap:10px}}.f-brand,.brand{{min-width:0}}.f-brand span,.brand{{line-height:1.08}}.f-actions,.links{{flex-shrink:0}}.f-btn,.button{{white-space:nowrap}}@media(max-width:760px){{main.cabinet>h2{{font-size:28px}}main.cabinet .summary-row{{align-items:center}}}}@media(max-width:520px){{.site-top .f-wrap,.wrap{{padding:0 14px}}.f-nav,nav{{gap:8px;height:64px}}.f-brand,.brand{{gap:7px;font-size:18px;letter-spacing:-.05em}}.f-brand .f-mark,.brand img{{width:36px;height:36px}}.f-actions,.links{{gap:6px}}.f-actions .f-btn:not(.primary){{display:inline-flex}}.f-btn,.button{{font-size:13px;padding:9px 12px}}.f-btn.primary,.button.primary{{padding-left:13px;padding-right:13px}}}}@media(max-width:390px){{.site-top .f-wrap,.wrap{{padding:0 10px}}.f-brand,.brand{{font-size:15px;gap:5px}}.f-brand .f-mark,.brand img{{width:30px;height:30px}}.f-btn,.button{{font-size:12px;padding:8px 10px}}.f-btn.primary,.button.primary{{padding-left:10px;padding-right:10px}}}}
.landing-page{{min-height:100dvh;display:grid;grid-template-rows:auto 1fr 36px}}.landing-page .f-hero{{min-height:calc(100dvh - 112px);border-radius:30px;padding:clamp(32px,5vh,54px) 38px;gap:32px}}.landing-page .f-hero h1{{font-size:clamp(36px,4.4vw,58px);font-weight:620;margin-bottom:18px}}.landing-page .f-kicker{{margin-bottom:16px}}.landing-page .f-lead{{font-size:15px;line-height:1.55;margin-bottom:23px}}.landing-page .f-btn.primary{{padding:10px 18px}}.landing-page .f-trust{{margin-top:22px}}.landing-page .f-preview{{max-width:440px;justify-self:end;padding:11px;border-radius:23px}}.landing-page .f-preview-inner{{padding:20px}}.landing-page .f-preview-top{{margin-bottom:20px}}.landing-page .f-key{{padding:13px}}.landing-page .f-stat{{padding:12px}}.landing-page .f-cabinet{{color:#fff;border-color:var(--blue);background:var(--blue);padding-left:21px;padding-right:21px}}.landing-page .f-cabinet:hover{{border-color:#0d49ea;background:#0d49ea;box-shadow:0 8px 22px #175cff28}}.f-footer{{height:36px;padding:0;display:flex;align-items:center;justify-content:center;background:#fff;color:var(--muted);font-size:11px;border-top:1px solid #eef1f6}}@media(max-width:850px){{.landing-page .f-hero{{min-height:calc(100dvh - 112px);display:flex;padding:32px 24px}}.landing-page .f-visual{{display:none}}.landing-page .f-hero>div:first-child{{width:min(560px,100%);margin:auto;text-align:center}}.landing-page .f-kicker,.landing-page .f-hero-actions,.landing-page .f-trust{{justify-content:center}}.landing-page .f-lead{{margin-left:auto;margin-right:auto}}}}@media(max-width:520px){{.landing-page{{grid-template-rows:auto 1fr 32px}}.landing-page .f-actions .f-cabinet{{display:inline-flex}}.landing-page .f-hero{{min-height:calc(100dvh - 96px);padding:26px 24px}}.landing-page .f-hero h1{{font-size:clamp(34px,10vw,43px)}}.landing-page .f-trust{{gap:10px 15px;margin-top:18px}}.f-footer{{height:32px;font-size:9px}}}}
.f-footer::before{{content:"Freedom VPN · Свобода быть собой в интернете"}}
</style><script>document.addEventListener('click',function(event){{const link=event.target.closest('a[href^="http"]');if(link&&!link.matches('[data-native-store]')&&window.Telegram?.WebApp?.openLink){{event.preventDefault();window.Telegram.WebApp.openLink(link.href)}}}});</script></head><body>{content}</body></html>"""


@router.get("/", response_class=HTMLResponse)
async def landing(db: AsyncSession = Depends(get_db)):
    all_plans = [plan for plan in await _plans(db) if _tier(plan)]
    body = f"""<div class=\"site-top landing-page\"><div class=\"f-wrap\"><nav class=\"f-nav\" aria-label=\"Основная навигация\"><a class=\"f-brand\" href=\"/\"><img class=\"f-mark\" src=\"/static/freedom-vpn-logo-web.webp\" width=\"50\" height=\"50\" alt=\"\"><span>Freedom <i>VPN</i></span></a><div class=\"f-actions\"><a class=\"f-btn f-cabinet\" href=\"/cabinet\">Кабинет</a></div></nav></div><section class=\"f-hero\"><div><div class=\"f-kicker\"><i></i>Свободный интернет без лишнего</div><h1>Быстрый и приватный интернет</h1><p class=\"f-lead\">Freedom VPN защищает ваше соединение и открывает доступ к сайтам и сервисам. Один аккаунт — все устройства, без рекламы и слежки.</p><div class=\"f-hero-actions\"><button class=\"f-btn primary\" type=\"button\" data-choose-plan>Выбрать подписку ↗</button></div><div class=\"f-trust\"><span><b class=\"f-dot\"></b>Сервера в 12 странах</span><span><b class=\"f-dot\"></b>Поддержка 24/7</span><span><b class=\"f-dot\"></b>Без логов</span></div></div><div class=\"f-visual\"><div class=\"f-preview\" aria-label=\"Предпросмотр личного кабинета\"><div class=\"f-preview-inner\"><div class=\"f-preview-top\"><div class=\"f-preview-brand\"><img class=\"f-mark\" src=\"/static/freedom-vpn-logo-web.webp\" width=\"38\" height=\"38\" alt=\""><span>Freedom <i>VPN</i></span></div><div class=\"f-status\"><b></b>Подключено</div></div><div class=\"f-key\"><div class=\"f-label\">Ключ доступа</div><div class=\"f-code\">freedom://vpn_7b91••••••••••••••••••••</div></div><div class=\"f-statrow\"><div class=\"f-stat\"><span class=\"f-label\">Локация</span><strong>Германия</strong></div><div class=\"f-stat\"><span class=\"f-label\">Задержка</span><strong>24 мс</strong></div></div><div class=\"f-devices\"><span class=\"f-device\">iOS</span><span class=\"f-device\">Android</span><span class=\"f-device\">Windows</span><span class=\"f-device\">Роутер</span></div></div></div></div></section><footer class=\"f-footer\" aria-label=\"Freedom VPN\"></footer></div>
<div class=\"modal\" id=\"register\"><div class=\"modal-card plan-modal\"><h3>Выберите подписку</h3><p class=\"muted\">Нажмите на срок, чтобы раскрыть остальные планы этой группы.</p><div class=\"tier-groups\">{_tier_selector(all_plans)}</div><input id=\"plan\" type=\"hidden\"><p id=\"result\"></p><div class=\"actions\"><button class=\"button\" type=\"button\" onclick=\"closeModal()\">Отмена</button><button class=\"button primary\" type=\"button\" onclick=\"checkout()\">Оплата</button></div></div></div>
<script>const modal=document.getElementById('register'),plan=document.getElementById('plan');function openPlans(id=''){{modal.classList.add('open');if(id)selectPlan(id)}}function selectPlan(id){{plan.value=id;document.querySelectorAll('[data-order-plan]').forEach(x=>x.classList.toggle('selected',x.dataset.orderPlan===String(id)));document.querySelectorAll('.tier-group').forEach(x=>x.classList.toggle('selected',!!x.querySelector('.selected')));document.querySelector(`[data-order-plan="${{id}}"]`)?.closest('.tier-group')?.classList.add('expanded')}}document.querySelectorAll('[data-choose-plan]').forEach(b=>b.onclick=()=>openPlans());document.querySelectorAll('[data-plan]').forEach(b=>b.onclick=()=>openPlans(b.dataset.plan));document.querySelectorAll('.plan-modal .tier-group').forEach(group=>group.onclick=event=>{{if(!event.target.closest('[data-order-plan]'))group.classList.toggle('expanded')}});document.querySelectorAll('[data-order-plan]').forEach(b=>b.onclick=event=>{{event.stopPropagation();selectPlan(b.dataset.orderPlan)}});function closeModal(){{modal.classList.remove('open')}}function checkout(){{const out=document.getElementById('result');if(!plan.value){{out.className='error';out.textContent='Выберите подписку';return}}location.href=`/cabinet?checkout=1&plan_id=${{encodeURIComponent(plan.value)}}#payment`}}</script>"""
    return HTMLResponse(_shell(body), headers=_headers())


def _code_digest(user_id: int, code: str) -> str:
    return hmac.new(
        settings.service_api_token.encode(),
        f"cabinet-login:{user_id}:{code}".encode(),
        sha256,
    ).hexdigest()


async def _issue_code(user: User, email_address: str, db: AsyncSession) -> tuple[datetime, str]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.cabinet_email_code_ttl_minutes
    )
    await db.execute(delete(CabinetLoginCode).where(CabinetLoginCode.user_id == user.id))
    db.add(
        CabinetLoginCode(
            user_id=user.id,
            code_hash=_code_digest(user.id, code),
            plain_code=code,
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
        await write_audit(
            db,
            action="email.cabinet_code.send",
            result="failed",
            actor_type="web",
            actor_id=str(user.id),
            resource_type="user",
            resource_id=user.id,
            details={
                "event_type": "cabinet_login_code_email_failed",
                "email": email_address,
                "ttl_minutes": settings.cabinet_email_code_ttl_minutes,
                "error": str(exc),
            },
        )
        logger.exception(
            "cabinet_login_code_email_failed",
            extra={
                "event": {
                    "event_type": "cabinet_login_code_email_failed",
                    "user_id": user.id,
                    "email": email_address,
                    "ttl_minutes": settings.cabinet_email_code_ttl_minutes,
                }
            },
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "cabinet_login_code_sent",
        extra={
            "event": {
                "event_type": "cabinet_login_code_sent",
                "user_id": user.id,
                "email": email_address,
                "ttl_minutes": settings.cabinet_email_code_ttl_minutes,
                "expires_at": expires.isoformat(),
            }
        },
    )
    await write_audit(
        db,
        action="email.cabinet_code.send",
        result="success",
        actor_type="web",
        actor_id=str(user.id),
        resource_type="user",
        resource_id=user.id,
        details={
            "event_type": "cabinet_login_code_sent",
            "email": email_address,
            "ttl_minutes": settings.cabinet_email_code_ttl_minutes,
            "expires_at": expires.isoformat(),
        },
    )
    return expires, code


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


def _payment_return_signature(payload: str) -> str:
    secret = settings.payment_webhook_secret or settings.service_api_token
    return hmac.new(secret.encode(), payload.encode(), sha256).hexdigest()


def _payment_return_token(user_id: int) -> str:
    payload = json.dumps(
        {
            "user_id": user_id,
            "exp": int(datetime.now(timezone.utc).timestamp()) + PAYMENT_RETURN_TOKEN_TTL_SECONDS,
            "nonce": secrets.token_urlsafe(12),
        },
        separators=(",", ":"),
    )
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_payment_return_signature(encoded)}"


def _verify_payment_return_token(token: str) -> int | None:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _payment_return_signature(encoded)):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        user_id = int(payload["user_id"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError, binascii.Error, json.JSONDecodeError):
        return None
    if expires_at < int(datetime.now(timezone.utc).timestamp()):
        return None
    return user_id


async def _create_cabinet_session(db: AsyncSession, user: User, response: Response) -> None:
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.cabinet_token_ttl_days)
    db.add(CabinetAccessToken(user_id=user.id, token_hash=_digest(raw), expires_at=expires))
    _set_cabinet_cookie(response, raw)


async def _merge_web_user_into_telegram_user(db: AsyncSession, web_user: User, telegram_user: User) -> User:
    if web_user.id == telegram_user.id:
        return telegram_user
    if web_user.telegram_id > 0 and web_user.telegram_id != telegram_user.telegram_id:
        raise HTTPException(status_code=409, detail="Этот email уже связан с другим Telegram аккаунтом")

    web_active_subscription = await db.scalar(
        select(Subscription.id)
        .where(Subscription.user_id == web_user.id, Subscription.status == "active")
        .limit(1)
    )
    telegram_active_subscription = await db.scalar(
        select(Subscription.id)
        .where(Subscription.user_id == telegram_user.id, Subscription.status == "active")
        .limit(1)
    )
    if web_active_subscription is not None and telegram_active_subscription is not None:
        raise HTTPException(
            status_code=409,
            detail="У web и Telegram аккаунтов уже есть активные подписки. Нужно объединить вручную в админке.",
        )

    web_email = web_user.email
    web_password_hash = web_user.password_hash
    web_user.email = None
    await db.flush()

    if web_email:
        telegram_user.email = web_email
    if web_password_hash and not telegram_user.password_hash:
        telegram_user.password_hash = web_password_hash

    for model in (
        CabinetAccessToken,
        CabinetLoginCode,
        Subscription,
        VPNClient,
        Payment,
        ClientDevice,
        ActivationCode,
    ):
        await db.execute(
            update(model)
            .where(model.user_id == web_user.id)
            .values(user_id=telegram_user.id)
        )
    try:
        await db.execute(
            update(AccessGrant)
            .where(AccessGrant.user_id == web_user.id)
            .values(user_id=telegram_user.id)
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Не удалось объединить пробный доступ автоматически. Нужно объединить вручную в админке.",
        ) from exc

    await db.delete(web_user)
    await db.flush()
    logger.info(
        "cabinet_users_merged",
        extra={
            "event": {
                "event_type": "cabinet_users_merged",
                "target_user_id": telegram_user.id,
                "merged_user_id": web_user.id,
                "telegram_id": telegram_user.telegram_id,
            }
        },
    )
    return telegram_user


@router.post("/web/register")
async def register(data: Registration, db: AsyncSession = Depends(get_db)):
    email_address = _normalize_email(data.email)
    if not EMAIL_RE.fullmatch(email_address):
        raise HTTPException(status_code=422, detail="Укажите корректный email")
    if data.plan_id is not None:
        plan = await db.get(Plan, data.plan_id)
        if plan is None or not plan.is_active or not plan.is_public:
            raise HTTPException(status_code=404, detail="Тариф не найден")
    linked_user = None
    if data.telegram_link_token:
        linked_user_id = verify_telegram_cabinet_link_token(data.telegram_link_token)
        if linked_user_id is None:
            raise HTTPException(status_code=401, detail="Ссылка Telegram устарела. Откройте web-кабинет из бота ещё раз.")
        linked_user = await db.get(User, linked_user_id)
        if linked_user is None or linked_user.status != "active":
            raise HTTPException(status_code=404, detail="Пользователь Telegram не найден")

    user = await db.scalar(select(User).where(User.email == email_address))
    if linked_user is not None:
        if user is not None and user.id != linked_user.id:
            user = await _merge_web_user_into_telegram_user(db, user, linked_user)
        else:
            user = linked_user
        linked_user.email = email_address
    elif user is None:
        user = User(telegram_id=-secrets.randbelow(9_000_000_000_000_000) - 1, email=email_address, status="active")
        db.add(user)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            user = await db.scalar(select(User).where(User.email == email_address))
            if user is None:
                raise HTTPException(status_code=409, detail="Не удалось создать пользователя")
    expires, _ = await _issue_code(user, email_address, db)
    return {"message": "Код для входа отправлен на почту", "expires_at": expires}


@router.post("/web/code/login")
async def email_code_login(data: EmailCodeLogin, db: AsyncSession = Depends(get_db)):
    email_address = _normalize_email(data.email)
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
    email_address = _normalize_email(data.email)
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
    email_address = _normalize_email(data.email)
    if not EMAIL_RE.fullmatch(email_address):
        raise HTTPException(status_code=422, detail="Укажите корректный email")
    user = await db.scalar(select(User).where(User.telegram_id == data.telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь Telegram не найден")
    owner = await db.scalar(select(User).where(User.email == email_address, User.id != user.id))
    if owner is not None:
        user = await _merge_web_user_into_telegram_user(db, owner, user)
    user.email = email_address
    expires, code = await _issue_code(user, email_address, db)
    token = telegram_cabinet_link_token(user.id)
    return {"message": "Код для входа отправлен на почту", "expires_at": expires, "code": code, "cabinet_url": f"/cabinet?tg={token}"}


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


@router.get("/cabinet/payment-return")
async def cabinet_payment_return(token: str, payment: str = "success", db: AsyncSession = Depends(get_db)):
    user_id = _verify_payment_return_token(token)
    if user_id is None:
        return RedirectResponse("/cabinet?payment=return_expired", status_code=303, headers=_headers())
    user = await db.get(User, user_id)
    if user is None or user.status != "active":
        return RedirectResponse("/cabinet?payment=return_expired", status_code=303, headers=_headers())
    response = RedirectResponse(f"/cabinet?payment={html.escape(payment)}", status_code=303, headers=_headers())
    await _create_cabinet_session(db, user, response)
    await db.commit()
    return response


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
        body = f'''<div class="wrap"><nav><a class="brand" href="/"><img src="/static/freedom-vpn-logo-web.webp" alt="">Freedom <i>VPN</i></a><a class="button" href="/">На главную</a></nav></div><main class="login-page"><section class="login-card"><h1>Вход в аккаунт</h1><p class="muted">Рады видеть вас снова.</p><label for="login-email">Email</label><input id="login-email" type="email" autocomplete="email" placeholder="you@example.com"><div class="login-tabs"><button class="active" type="button" data-login-mode="email">Код из письма</button><button type="button" data-login-mode="password">Пароль</button></div><div class="login-mode" data-mode-panel="email"><p class="muted" style="margin-top:22px">Пришлём шестизначный одноразовый код — пароль не нужен.</p><button class="button gradient" type="button" onclick="requestLogin()">Получить код на email</button><label for="login-code">Код из письма</label><input id="login-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{{6}}" placeholder="000000"><button class="button gradient" type="button" onclick="codeLogin()">Войти по коду</button></div><div class="login-mode" data-mode-panel="password" hidden><div class="password-heading"><label for="login-password">Пароль</label><button type="button" onclick="requestPasswordReset()">Получить код</button></div><input id="login-password" type="password" autocomplete="current-password" minlength="8" placeholder="••••••••"><button class="button gradient" type="button" onclick="passwordLogin()">Войти</button></div><p id="login-result"></p>{_temporary_registration_button()}</section></main><script>const params=new URLSearchParams(location.search);const telegramLinkToken=params.get('tg')||'';const paymentReturn=params.get('payment');const paymentReturnToken=sessionStorage.getItem('freedom_payment_return_token');if(paymentReturn&&paymentReturnToken){{sessionStorage.removeItem('freedom_payment_return_token');location.replace('/cabinet/payment-return?payment='+encodeURIComponent(paymentReturn)+'&token='+encodeURIComponent(paymentReturnToken))}}const tabs=document.querySelectorAll('[data-login-mode]');function selectLoginMode(mode){{tabs.forEach(x=>x.classList.toggle('active',x.dataset.loginMode===mode));document.querySelectorAll('[data-mode-panel]').forEach(panel=>panel.hidden=panel.dataset.modePanel!==mode)}}tabs.forEach(tab=>tab.onclick=()=>selectLoginMode(tab.dataset.loginMode));async function requestLogin(){{const out=document.getElementById('login-result');out.className='';out.textContent='Отправляем код…';const body={{email:document.getElementById('login-email').value}};if(telegramLinkToken)body.telegram_link_token=telegramLinkToken;const r=await fetch('/web/register',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});const d=await r.json();out.className=r.ok?'success':'error';out.textContent=r.ok?d.message:(d.detail||'Ошибка отправки');if(r.ok)document.getElementById('login-code').focus()}}async function requestPasswordReset(){{selectLoginMode('email');await requestLogin()}}async function codeLogin(){{const out=document.getElementById('login-result');out.className='';out.textContent='Проверяем код…';const r=await fetch('/web/code/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:document.getElementById('login-email').value,code:document.getElementById('login-code').value}})}});const d=await r.json();if(r.ok){{location.reload();return}}out.className='error';out.textContent=d.detail||'Ошибка входа'}}async function passwordLogin(){{const out=document.getElementById('login-result');out.className='';out.textContent='Проверяем…';const r=await fetch('/web/password/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:document.getElementById('login-email').value,password:document.getElementById('login-password').value}})}});const d=await r.json();if(r.ok){{location.reload();return}}out.className='error';out.textContent=d.detail||'Ошибка входа'}};</script>'''
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
    traffic_remaining_bytes = None
    traffic_limit_bytes = None
    traffic_used_bytes = None
    if client and client.traffic_limit_gb:
        traffic_limit_bytes = client.traffic_limit_gb * 1024 * 1024 * 1024
        traffic_remaining_bytes = traffic_limit_bytes
        if node and config:
            try:
                traffic_stats = await ThreeXUIClient(config.config.get("api_address")).get_client_traffic(f"vpn-{client.id}")
                traffic_used_bytes = int(traffic_stats.get("up", 0)) + int(traffic_stats.get("down", 0))
                total_bytes = int(traffic_stats.get("total", 0)) or traffic_limit_bytes
                traffic_limit_bytes = total_bytes
                traffic_remaining_bytes = max(total_bytes - traffic_used_bytes, 0)
            except (ThreeXUIError, TypeError, ValueError):
                logger.warning(
                    "cabinet_traffic_lookup_failed",
                    exc_info=True,
                    extra={"event_type": "cabinet_traffic_lookup_failed", "client_id": client.id},
                )
    traffic_exhausted = bool(traffic_limit_bytes and traffic_remaining_bytes is not None and traffic_remaining_bytes <= 0)
    active = bool(
        subscription
        and subscription.status == "active"
        and subscription.expires_at.replace(tzinfo=subscription.expires_at.tzinfo or timezone.utc) > now
        and not traffic_exhausted
    )
    days = max(0, int(((subscription.expires_at.replace(tzinfo=subscription.expires_at.tzinfo or timezone.utc) - now).total_seconds() + 86399) // 86400)) if subscription else 0
    if client and not client.traffic_limit_gb:
        traffic = "Без ограничений"
    elif traffic_remaining_bytes is not None and traffic_limit_bytes:
        remaining_gb = traffic_remaining_bytes / 1024 / 1024 / 1024
        limit_gb = traffic_limit_bytes / 1024 / 1024 / 1024
        traffic = f"{_format_gb(remaining_gb)} из {_format_gb(limit_gb)}"
    elif client:
        traffic = f"{client.traffic_limit_gb} ГБ"
    else:
        traffic = "—"
    devices = "Без ограничений" if client and not client.max_connections else (str(client.max_connections) if client else "—")
    public_plans = await _plans(db)
    packages = await _plan_packages(db)
    packages_by_id = {item.id: item for item in packages}
    methods = list((await db.execute(select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).order_by(PaymentMethod.sort_order))).scalars())
    payments = list((await db.execute(select(Payment).where(Payment.user_id == user.id).order_by(Payment.id.desc()).limit(10))).scalars())
    requested_plan = next((item for item in public_plans if item.id == plan_id), None)
    current_plan = next((item for item in public_plans if plan and item.id == plan.id), None)
    initial_plan = requested_plan or current_plan or (public_plans[0] if public_plans else None)
    initial_plan_id = initial_plan.id if initial_plan else 0
    method_radios = "".join(
        f'<label class="method-radio"><input type="radio" name="payment-method" value="{html.escape(method.code)}"'
        f'{" checked" if index == 0 else ""}><span class="radio-mark" aria-hidden="true"></span>'
        f'<span>{html.escape(_clean_payment_method_name(method.name))}</span></label>'
        for index, method in enumerate(methods)
    )
    plan_payload = {
        item.id: {
            "name": _clean_plan_name(item.name),
            "package": _package_label(item, packages_by_id),
            "duration_days": item.duration_days,
            "price": _format_money(item.price, item.currency).rsplit(" ", 1)[0],
            "currency": item.currency,
        }
        for item in public_plans
    }
    method_payload = {
        item.code: {
            "name": _clean_payment_method_name(item.name),
            "url": item.url or "",
            "manual": item.code in MANUAL_PAYMENT_METHODS,
        }
        for item in methods
    }
    plan_json = json.dumps(plan_payload, ensure_ascii=False)
    method_json = json.dumps(method_payload, ensure_ascii=False)
    payment_rows = _cabinet_payment_rows(payments)
    status_class = "status" if active else "status inactive"
    status_text = "Активна" if active else "Не активна"
    current_plan_name = _cabinet_plan_name(plan, packages_by_id)
    server_flag, server_name = _server_label(node)
    tariff_selector = _tier_selector(public_plans, packages)
    key_block = '<p class="muted">Ключ появится после подтверждения оплаты и выдачи подписки.</p>'
    if vpn_uri:
        scheme = vpn_uri.partition("://")[0] or "vpn"
        key_block = (
            '<div class="key-row">'
            f'<div class="key"><code id="vpn-key" data-value="{html.escape(vpn_uri, quote=True)}">{html.escape(scheme)}://••••••••••••</code></div>'
            '<button class="button copy-button" type="button" onclick="copyKey(this)" aria-label="Скопировать VPN-ключ"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="11" height="11" rx="2"></rect><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"></path></svg>Копировать ключ</button>'
            '</div>'
        )
    body = f"""
<div class="cabinet-page">
<header class="cabinet-header"><a class="brand cabinet-brand" href="/"><img src="/static/freedom-vpn-logo-web.webp" alt=""><span>Freedom</span><i>VPN</i></a><form method="post" action="/cabinet/logout"><button class="cabinet-logout" type="submit">Выйти</button></form></header>
<main class="cabinet cabinet-shell">
<div class="cabinet-title"><h1>Личный кабинет</h1><p>{html.escape(_masked_email(user.email))}</p></div>
<div class="cabinet-stack">
<section class="cabinet-card subscription-card"><div class="card-heading"><div><h2>Подписка</h2><p class="current-plan">{html.escape(current_plan_name)}</p></div><span class="status-badge {status_class}">{status_text}</span></div><div class="subscription-stats"><div><small>Осталось</small><strong>{days} дн.</strong></div><div><small>Трафик</small><strong>{traffic}</strong></div><div><small>Подключения</small><strong>{devices}</strong></div></div></section>
<section class="cabinet-card renew-card" id="payment"><div class="renew-heading"><h2>Продлить подписку</h2><p id="renew-subtitle">Выберите способ оплаты</p><div class="renew-tabs" role="tablist" aria-label="Продление подписки"><span class="tab-indicator" aria-hidden="true"></span><button class="renew-tab active" id="payment-tab" type="button" role="tab" aria-selected="true" aria-controls="payment-panel" onclick="setRenewMode('payment')">Оплата</button><button class="renew-tab" id="tariffs-tab" type="button" role="tab" aria-selected="false" aria-controls="tariffs-panel" onclick="setRenewMode('tariffs')">Изменить тариф</button></div></div><input type="hidden" id="order-plan" value="{initial_plan_id}">
<div id="payment-panel" role="tabpanel" aria-labelledby="payment-tab" data-renew-panel="payment"><div class="payment-method-control"><button class="payment-picker" id="payment-picker" type="button" aria-haspopup="true" aria-expanded="false" aria-controls="payment-method-menu" onclick="toggleMethodMenu()"><span class="payment-method-icon" id="payment-method-icon" data-kind="qr" aria-hidden="true"><svg class="method-icon icon-qr" viewBox="0 0 24 24"><path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h3v3h-3zM18 14h3v7h-3zM14 18h3v3h-3z"></path><path class="qr-cut" d="M5 5h3v3H5zM16 5h3v3h-3zM5 16h3v3H5z"></path></svg><svg class="method-icon icon-card" viewBox="0 0 24 24"><rect x="2.5" y="5" width="19" height="14" rx="2.5"></rect><path d="M3 9h18"></path></svg><span class="method-icon icon-crypto">₿</span></span><span class="payment-method-copy"><small>Способ оплаты</small><strong id="payment-method-name">Способ оплаты</strong><span id="payment-method-hint">Нажмите, чтобы выбрать</span></span><span class="payment-check" aria-hidden="true">✓</span><span class="payment-chevron" aria-hidden="true">›</span></button><fieldset class="method-menu" id="payment-method-menu" hidden><legend class="sr-only">Способ оплаты</legend>{method_radios or '<p class="muted">Способы оплаты временно недоступны.</p>'}</fieldset></div><div class="renew-order"><div><small>Тариф</small><strong id="renew-plan-name">—</strong></div><div class="renew-amount"><small>К оплате</small><strong id="renew-amount">—</strong></div></div><button class="pay-button" id="pay-button" type="button" onclick="openPayment()">Оплатить</button></div>
<div id="tariffs-panel" role="tabpanel" aria-labelledby="tariffs-tab" data-renew-panel="tariffs" hidden><div class="inline-tariffs">{tariff_selector or '<p class="muted">Тарифы временно недоступны.</p>'}</div><p class="tariff-help">Выберите пакет и срок — сумма обновится автоматически.</p></div></section>
<section class="cabinet-card connection-card"><h2>Подключение</h2>{key_block}<p class="server-line"><span aria-hidden="true">{server_flag}</span> Сервер: {html.escape(server_name)}</p></section>
<section class="cabinet-card apps-card"><h2>Скачать приложение</h2><div class="app-grid"><a class="app-button" href="https://github.com/amnezia-vpn/amnezia-client/releases/download/4.8.10.0/AmneziaVPN_4.8.10.0_windows_x64.exe"><svg class="app-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 4h8v8H3zM13 3h8v9h-8zM3 14h8v7H3zM13 14h8v8h-8z"></path></svg>Windows</a><a class="app-button" href="https://github.com/amnezia-vpn/amnezia-client/releases/download/4.8.10.0/AmneziaVPN_4.8.10.0_macos.zip"><svg class="app-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="12" rx="2"></rect><path d="M2 19h20"></path></svg>macOS</a><a class="app-button" data-native-store href="https://play.google.com/store/apps/details?id=llc.itdev.incy" target="_blank" rel="noopener noreferrer"><svg class="app-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 9h10v9H7zM9 5 7 2M15 5l2-3M5 10v6M19 10v6M9 18v4M15 18v4"></path><circle cx="10" cy="8" r=".7"></circle><circle cx="14" cy="8" r=".7"></circle></svg>Android</a><a class="app-button" data-native-store href="https://apps.apple.com/ca/app/incy/id6756943388" target="_blank" rel="noopener noreferrer"><svg class="app-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="2" width="10" height="20" rx="2.5"></rect><path d="M10 5h4M11 19h2"></path></svg>iOS</a></div></section>
<section class="cabinet-card payments-card"><h2>Последние платежи</h2><div class="payment-list">{payment_rows}</div></section>
<section class="cabinet-card password-card"><h2>Пароль для входа</h2><p class="muted">Задайте или смените пароль для входа в кабинет без кода из письма.</p><div class="password-form"><label class="sr-only" for="cabinet-password">Новый пароль</label><input id="cabinet-password" type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="Новый пароль — не менее 8 символов"><button class="button primary" type="button" onclick="saveCabinetPassword()">Сохранить пароль</button></div><p id="password-result"></p></section>
</div></main></div>
<div class="modal" id="payment-modal"><div class="modal-card payment-modal-card"><h3>Оплата</h3><p class="muted">Проверьте сумму и выбранный способ оплаты перед продолжением.</p><div id="payment-summary-modal" class="payment-summary"></div><div id="order-result"></div><div class="actions"><button class="button" type="button" onclick="closePayment()">Назад</button><button class="button primary" type="button" onclick="confirmPayment()">Продлить</button></div></div></div>
<style>
.cabinet-page{{min-height:100vh;background:radial-gradient(circle at 50% 0,#eaf2ff 0,#f5f8fc 38%,#f1f5fa 100%);padding:0 18px 70px}}.cabinet-header{{width:min(900px,100%);height:84px;margin:0 auto;display:flex;align-items:center;justify-content:space-between}}.cabinet-header .brand{{font-size:24px}}.cabinet-header .brand img{{width:52px;height:52px}}.cabinet-logout{{border:0;background:transparent;color:var(--blue);font:inherit;font-size:17px;font-weight:750;cursor:pointer;padding:10px}}.cabinet-shell{{width:min(900px,100%);margin:0 auto;padding:16px 0 0}}.cabinet-title{{margin:0 4px 24px}}.cabinet-title h1{{font-size:42px;line-height:1.05;letter-spacing:-.05em;margin:0 0 3px}}.cabinet-title p{{margin:0;color:#65728a;font-size:22px;font-weight:650}}.cabinet-stack{{display:grid;gap:18px}}.cabinet-card{{background:#fff;border:1px solid #e3e9f2;border-radius:20px;padding:24px 28px;box-shadow:0 12px 36px #1c4b8d0a;min-width:0}}.cabinet-card h2{{font-size:27px;line-height:1.15;letter-spacing:-.035em;margin:0}}.card-heading{{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}}.current-plan{{color:var(--muted);margin:6px 0 0;font-weight:650}}.status-badge{{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;padding:8px 22px;background:#c9f7df;color:#128246;font-weight:800;white-space:nowrap}}.status-badge.inactive{{background:#fee2e2;color:#c62c2c}}.subscription-stats{{display:grid;grid-template-columns:repeat(3,1fr);margin-top:22px}}.subscription-stats>div{{padding:2px 28px 2px 0;min-width:0}}.subscription-stats>div+div{{border-left:1px solid #dce4ef;padding-left:40px}}.subscription-stats small,.renew-order small{{display:block;color:#68758e;font-size:15px;margin-bottom:2px}}.subscription-stats strong{{display:block;font-size:27px;letter-spacing:-.035em;overflow-wrap:anywhere}}.renew-card{{padding:0 20px 20px;border:3px solid var(--blue);overflow:hidden}}.renew-heading{{margin:0 -20px 12px;padding:22px 28px 18px;background:linear-gradient(110deg,#edf5ff,#fff)}}.renew-heading p{{color:#5f6d87;font-size:17px;margin:4px 0 0}}.payment-picker{{position:relative;display:grid;grid-template-columns:68px minmax(0,1fr) 44px 22px;align-items:center;gap:14px;min-height:112px;border:2px solid var(--blue);border-radius:17px;padding:18px 20px;background:#fff;box-shadow:0 5px 18px #175cff10}}.payment-picker select{{position:absolute;inset:0;width:100%;height:100%;margin:0;opacity:0;cursor:pointer;z-index:2}}.payment-method-icon{{display:flex;align-items:center;justify-content:center;width:56px;height:56px;border-radius:14px;background:#edf4ff;color:#071534;font-size:35px;font-weight:900}}.payment-method-copy{{display:flex;flex-direction:column;min-width:0}}.payment-method-copy strong{{font-size:23px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.payment-method-copy small{{color:#68758e;font-size:14px}}.payment-check{{display:grid;place-items:center;width:42px;height:42px;border-radius:50%;background:var(--blue);color:#fff;font-size:24px;font-weight:800}}.payment-chevron{{font-size:40px;line-height:1;color:#0a1733}}.renew-order{{display:grid;grid-template-columns:1fr auto;align-items:end;gap:22px;margin-top:18px;padding:16px 20px;border:1px solid #e4eaf3;border-radius:15px;background:#fff}}.renew-order strong{{font-size:20px}}.renew-amount{{text-align:right}}.renew-amount strong{{font-size:26px}}.pay-button{{display:block;width:100%;border:0;border-radius:13px;margin-top:14px;padding:14px 18px;background:var(--blue);color:#fff;font:inherit;font-size:20px;font-weight:750;cursor:pointer;box-shadow:0 10px 24px #175cff26}}.change-plan{{display:block;text-align:center;color:var(--blue);font-size:17px;font-weight:750;margin-top:12px}}.key-row{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;align-items:center;margin-top:18px}}.key-row .key{{min-width:0;background:#f1f5fa;border:0;padding:16px 18px}}.copy-button{{border-color:var(--blue);color:var(--blue);border-radius:12px;white-space:nowrap}}.copy-button span{{font-size:19px;margin-right:7px}}.server-line{{margin:14px 0 0;color:#65728a}}.server-line span{{font-size:21px;margin-right:7px}}.app-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:18px}}.app-button{{display:flex;align-items:center;justify-content:center;gap:11px;border:1px solid #b9c8dd;border-radius:12px;padding:13px 10px;background:#fff;font-weight:650;transition:.2s}}.app-button:hover{{border-color:var(--blue);color:var(--blue)}}.app-button span{{font-size:22px;color:#061541}}.payment-list{{margin-top:12px}}.payment-row{{display:grid;grid-template-columns:90px 1fr 140px 120px;align-items:center;gap:12px;padding:12px 2px;border-bottom:1px solid #dfe6f0}}.payment-row:last-child{{border-bottom:0}}.payment-row time{{color:#66738c;text-align:right}}.payment-state{{justify-self:center;min-width:112px;border-radius:999px;padding:6px 14px;text-align:center;background:#e7edf5;color:#465166}}.payment-state.paid{{background:#c9f7df;color:#128246}}.payment-state.failed{{background:#fee2e2;color:#bd2929}}.payment-empty{{padding:16px 0}}.password-card>.muted{{margin:8px 0 18px}}.password-form{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px}}.password-form input{{margin:0;background:#f6f8fb;border-color:#dfe6ef;padding:14px 16px}}.password-form .button{{border-radius:12px}}.sr-only{{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}.payment-summary{{margin:16px 0;padding:16px;border:1px solid var(--line);border-radius:16px;background:#fff}}.payment-summary h3{{margin:0 0 12px;text-align:center}}.summary-row{{display:flex;justify-content:space-between;gap:16px;padding:9px 0;border-bottom:1px solid #e7edf8}}.summary-row:last-child{{border-bottom:0}}.summary-row span{{color:var(--muted)}}.summary-row b{{text-align:right}}.summary-total b{{font-size:20px;color:var(--ink)}}.payment-modal-card{{width:min(620px,100%)}}#order-result img{{display:block;margin:12px 0;border-radius:16px;border:1px solid var(--line)}}#order-result input[type=file]{{margin:12px 0}}
@media(max-width:700px){{.cabinet-page{{padding:0 12px 44px}}.cabinet-header{{height:68px}}.cabinet-header .brand{{font-size:19px}}.cabinet-header .brand img{{width:40px;height:40px}}.cabinet-shell{{padding-top:14px}}.cabinet-title{{margin-bottom:18px}}.cabinet-title h1{{font-size:34px}}.cabinet-title p{{font-size:17px}}.cabinet-card{{padding:20px}}.cabinet-card h2{{font-size:24px}}.subscription-stats{{grid-template-columns:1fr;gap:14px}}.subscription-stats>div{{padding:0}}.subscription-stats>div+div{{border-left:0;border-top:1px solid #e2e8f1;padding:13px 0 0}}.subscription-stats strong{{font-size:23px}}.renew-card{{padding:0 14px 16px}}.renew-heading{{margin:0 -14px 12px;padding:18px}}.payment-picker{{grid-template-columns:48px minmax(0,1fr) 36px 14px;gap:10px;min-height:92px;padding:14px}}.payment-method-icon{{width:44px;height:44px;font-size:28px}}.payment-method-copy strong{{font-size:19px}}.payment-method-copy small{{font-size:12px}}.payment-check{{width:34px;height:34px;font-size:20px}}.payment-chevron{{font-size:30px}}.renew-order{{padding:14px;gap:10px}}.renew-order strong{{font-size:17px}}.renew-amount strong{{font-size:21px}}.key-row,.password-form{{grid-template-columns:1fr}}.copy-button,.password-form .button{{width:100%}}.app-grid{{grid-template-columns:repeat(2,1fr)}}.payment-row{{grid-template-columns:60px 1fr auto;gap:8px}}.payment-row time{{grid-column:2/4;text-align:left;font-size:13px}}.payment-state{{min-width:0;padding:5px 10px}}}}
@media(max-width:390px){{.cabinet-page{{padding-left:8px;padding-right:8px}}.cabinet-card{{padding:17px}}.status-badge{{padding:7px 12px}}.payment-picker{{grid-template-columns:42px minmax(0,1fr) 31px 10px;padding:11px;gap:7px}}.payment-method-icon{{width:38px;height:38px;font-size:24px}}.payment-method-copy strong{{font-size:17px}}.payment-check{{width:30px;height:30px;font-size:17px}}.renew-card{{padding-left:10px;padding-right:10px}}.renew-heading{{margin-left:-10px;margin-right:-10px}}}}
</style>
<style>
.cabinet-page{{--cabinet-bg:#edf1f7;--cabinet-card:#fff;--cabinet-text:#142039;--cabinet-muted:#64708a;--cabinet-line:#d8e0ee;--cabinet-blue:#245bff;--cabinet-blue-soft:#edf4ff;--cabinet-green:#14834b;--cabinet-green-soft:#c8f5dc;--cabinet-radius:20px;--cabinet-gap:14px;--cabinet-shadow:0 8px 24px #17345f0b;min-height:100vh;background:var(--cabinet-bg);color:var(--cabinet-text);padding:0 12px 34px;font-family:ui-rounded,"SF Pro Rounded","Avenir Next",Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;font-weight:450}}.cabinet-header,.cabinet-shell{{width:min(760px,100%);margin:0 auto}}.cabinet-header{{height:60px}}.cabinet-brand{{gap:7px!important;font-size:20px!important;letter-spacing:-.035em!important}}.cabinet-brand img{{width:38px!important;height:38px!important;mix-blend-mode:multiply}}.cabinet-brand span{{font-weight:720}}.cabinet-brand i{{font-weight:430;letter-spacing:-.025em}}.cabinet-logout{{font-size:14px;font-weight:650;min-width:44px;min-height:44px}}.cabinet-shell{{padding:7px 0 0!important}}.cabinet-title{{margin:0 3px 13px}}.cabinet-title h1{{font-size:29px;font-weight:680;letter-spacing:-.045em;margin:0;line-height:1.08}}.cabinet-title p{{font-size:14px;font-weight:520;margin-top:2px;color:var(--cabinet-muted)}}.cabinet-stack{{gap:var(--cabinet-gap)}}.cabinet-card{{padding:16px 18px;border:1px solid var(--cabinet-line);border-radius:var(--cabinet-radius);box-shadow:var(--cabinet-shadow);background:var(--cabinet-card)}}.cabinet-card h2{{font-size:20px;font-weight:680;letter-spacing:-.025em}}.card-heading{{align-items:center}}.current-plan{{font-size:13px;font-weight:520;margin:2px 0 0}}.status-badge{{padding:5px 13px;font-size:12px;font-weight:680;background:var(--cabinet-green-soft);color:var(--cabinet-green)}}.status-badge.inactive{{background:#fee2e2;color:#b42318}}.subscription-stats{{grid-template-columns:repeat(3,minmax(0,1fr));margin-top:13px}}.subscription-stats>div{{padding:0 14px 0 0}}.subscription-stats>div+div{{padding:0 14px;border-left:1px solid var(--cabinet-line)}}.subscription-stats small,.renew-order small{{font-size:11px;color:var(--cabinet-muted);margin:0}}.subscription-stats strong{{font-size:18px;font-weight:650;line-height:1.25;margin-top:1px}}.renew-card{{padding:0 14px 14px;border:2px solid var(--cabinet-blue);border-radius:var(--cabinet-radius);overflow:visible;transition:border-color .22s,box-shadow .22s}}.renew-heading{{display:grid;grid-template-columns:minmax(0,1fr) 238px;align-items:center;gap:14px;margin:0 -14px 10px;padding:13px 14px;background:#eaf2ff;border-radius:17px 17px 0 0}}.renew-heading h2{{font-size:19px}}.renew-heading p{{font-size:12px;margin:1px 0 0;color:var(--cabinet-muted)}}.renew-tabs{{position:relative;display:grid;grid-template-columns:1fr 1fr;padding:3px;border:1px solid #c8d7f2;border-radius:11px;background:#fff;overflow:hidden}}.tab-indicator{{position:absolute;left:3px;top:3px;width:calc(50% - 3px);height:calc(100% - 6px);border-radius:8px;background:var(--cabinet-blue);transition:transform .22s ease}}.renew-card.tariffs-mode .tab-indicator{{transform:translateX(100%)}}.renew-tab{{position:relative;z-index:1;min-height:34px;border:0;background:transparent;color:var(--cabinet-muted);font:inherit;font-size:11px;font-weight:650;cursor:pointer;border-radius:8px;transition:color .22s}}.renew-tab.active{{color:#fff}}.payment-method-control{{position:relative}}.payment-picker{{width:100%;display:grid;grid-template-columns:46px minmax(0,1fr) 30px 14px;align-items:center;gap:10px;min-height:68px;border:2px solid var(--cabinet-blue);border-radius:14px;padding:8px 12px;background:var(--cabinet-blue-soft);color:var(--cabinet-text);text-align:left;font:inherit;cursor:pointer;box-shadow:none}}.payment-picker:hover{{background:#e5efff}}.payment-picker:active{{transform:scale(.997)}}.payment-picker:focus-visible,.renew-tab:focus-visible,.method-radio:focus-within,.pay-button:focus-visible,.app-button:focus-visible,.copy-button:focus-visible,.password-form input:focus-visible,.password-form button:focus-visible{{outline:3px solid #85a7ff;outline-offset:2px}}.payment-method-icon{{width:40px;height:40px;border-radius:10px;background:#fff;color:var(--cabinet-text);font-size:24px}}.payment-method-icon .method-icon{{display:none;width:23px;height:23px}}.payment-method-icon[data-kind="qr"] .icon-qr,.payment-method-icon[data-kind="card"] .icon-card,.payment-method-icon[data-kind="crypto"] .icon-crypto{{display:block}}.payment-method-icon svg{{fill:var(--cabinet-text);stroke:var(--cabinet-text);stroke-width:1.7}}.payment-method-icon .icon-qr{{stroke:none}}.payment-method-icon .qr-cut{{fill:#fff}}.payment-method-copy small{{order:-1;color:var(--cabinet-muted);font-size:10px;line-height:1.2}}.payment-method-copy strong{{font-size:16px;font-weight:680;line-height:1.25}}.payment-method-copy span{{color:var(--cabinet-muted);font-size:11px;line-height:1.25}}.payment-check{{width:28px;height:28px;font-size:17px;background:var(--cabinet-blue)}}.payment-chevron{{font-size:25px}}.payment-method-control.single-method .payment-chevron{{display:none}}.payment-method-control.single-method .payment-picker{{grid-template-columns:46px minmax(0,1fr) 30px;cursor:default}}.method-menu{{position:absolute;z-index:8;top:calc(100% + 6px);left:0;right:0;margin:0;padding:6px;border:1px solid var(--cabinet-line);border-radius:13px;background:#fff;box-shadow:0 16px 36px #14203924}}.method-radio{{display:flex;align-items:center;gap:10px;min-height:44px;padding:8px 10px;border-radius:9px;cursor:pointer}}.method-radio:hover{{background:var(--cabinet-blue-soft)}}.method-radio input{{position:absolute;opacity:0;width:1px;height:1px}}.radio-mark{{width:18px;height:18px;border:2px solid #9aabc5;border-radius:50%;display:grid;place-items:center}}.method-radio input:checked+.radio-mark{{border:5px solid var(--cabinet-blue)}}.method-radio span:last-child{{font-weight:590}}.renew-order{{margin-top:9px;padding:9px 12px;border:1px solid var(--cabinet-line);border-radius:12px;min-height:49px}}.renew-order strong{{font-size:14px;font-weight:650}}.renew-amount strong{{font-size:18px}}.pay-button{{min-height:48px;margin-top:9px;padding:10px 16px;border-radius:11px;background:var(--cabinet-blue);font-size:15px;font-weight:680;box-shadow:none;transition:background .16s,transform .08s,opacity .16s}}.pay-button:hover{{background:#1749e8}}.pay-button:active{{transform:translateY(1px) scale(.997)}}.pay-button:disabled,.pay-button.loading{{opacity:.58;cursor:not-allowed}}[data-renew-panel="tariffs"]{{animation:panel-in .2s ease}}@keyframes panel-in{{from{{opacity:.35;transform:translateY(4px)}}to{{opacity:1;transform:none}}}}.inline-tariffs{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}}.inline-tariffs .tier-group{{min-width:0;padding:10px;border:1px solid var(--cabinet-line);border-radius:12px;background:#f8faff;cursor:default}}.inline-tariffs .tier-group.selected{{padding:9px;border:2px solid var(--cabinet-blue);background:var(--cabinet-blue-soft)}}.inline-tariffs .tier-group h3{{font-size:14px;margin:0;font-weight:680}}.inline-tariffs .tier-group p{{min-height:31px;margin:2px 0 7px;font-size:10px;line-height:1.35}}.inline-tariffs .duration-buttons{{display:grid;gap:4px;margin:0}}.inline-tariffs .duration{{display:flex!important;min-height:32px;align-items:center;justify-content:center;padding:5px 6px;border-radius:8px;font-size:10px;line-height:1.2;text-align:center}}.inline-tariffs .duration.selected{{background:var(--cabinet-blue);border-color:var(--cabinet-blue);color:#fff}}.tariff-help{{margin:7px 2px 0;color:var(--cabinet-muted);font-size:10px;text-align:center}}.key-row{{grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:9px}}.key-row .key{{padding:10px 12px;border-radius:10px;background:#f2f5fa}}.key-row .key code{{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px;color:#34415b}}.copy-button{{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:44px;padding:8px 12px;border-radius:10px;font-size:12px}}.copy-button svg{{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.8}}.server-line{{display:flex;align-items:center;gap:6px;margin:8px 0 0;font-size:12px}}.server-line span{{font-size:17px;margin:0}}.app-grid{{grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:10px}}.app-button{{min-height:44px;gap:6px;padding:7px 5px;border-radius:10px;font-size:11px;font-weight:580}}.app-icon{{width:17px;height:17px;fill:var(--cabinet-text);stroke:var(--cabinet-text);stroke-width:1.7}}.apps-card h2,.payments-card h2,.password-card h2,.connection-card h2{{font-size:18px}}.payment-list{{margin-top:7px}}.payment-row{{grid-template-columns:55px minmax(70px,1fr) 92px 76px;gap:7px;min-height:39px;padding:6px 1px;font-size:11px}}.payment-row time{{text-align:right;font-size:10px}}.payment-state{{justify-self:end;min-width:76px;padding:4px 8px;font-size:10px}}.payment-empty{{padding:8px 0}}.password-card>.muted{{margin:3px 0 9px;font-size:11px}}.password-form{{grid-template-columns:minmax(0,1fr) auto;gap:8px}}.password-form input{{min-height:44px;padding:9px 12px;font-size:12px;border-radius:10px}}.password-form .button{{min-height:44px;padding:8px 13px;border-radius:10px;font-size:12px}}.payment-summary{{padding:12px;margin:12px 0}}.modal-card{{font-family:inherit}}@media(max-width:620px){{.cabinet-page{{--cabinet-gap:11px;padding:0 8px 24px}}.cabinet-header{{height:54px}}.cabinet-brand{{font-size:17px!important}}.cabinet-brand img{{width:32px!important;height:32px!important}}.cabinet-shell{{padding-top:4px!important}}.cabinet-title{{margin-bottom:9px}}.cabinet-title h1{{font-size:24px}}.cabinet-title p{{font-size:12px}}.cabinet-card{{padding:12px 13px;border-radius:16px}}.cabinet-card h2{{font-size:17px}}.status-badge{{font-size:10px;padding:4px 9px}}.subscription-stats{{gap:0;margin-top:9px}}.subscription-stats>div{{padding:0 7px}}.subscription-stats>div+div{{padding:0 7px}}.subscription-stats small{{font-size:9px}}.subscription-stats strong{{font-size:14px}}.renew-card{{padding:0 9px 9px;border-radius:16px}}.renew-heading{{grid-template-columns:1fr 210px;margin:0 -9px 7px;padding:8px 9px;border-radius:13px 13px 0 0}}.renew-heading h2{{font-size:15px}}.renew-heading p{{font-size:9px}}.renew-tabs{{padding:2px}}.tab-indicator{{left:2px;top:2px;width:calc(50% - 2px);height:calc(100% - 4px)}}.renew-tab{{min-height:30px;font-size:9px}}.payment-picker{{min-height:60px;grid-template-columns:39px minmax(0,1fr) 26px 11px;gap:7px;padding:7px 9px;border-radius:11px}}.payment-method-icon{{width:34px;height:34px}}.payment-method-icon .method-icon{{width:20px;height:20px}}.payment-method-copy strong{{font-size:14px}}.payment-method-copy span{{font-size:9px}}.payment-check{{width:24px;height:24px;font-size:14px}}.renew-order{{min-height:43px;padding:7px 9px}}.renew-order strong{{font-size:12px}}.renew-amount strong{{font-size:15px}}.pay-button{{min-height:46px;margin-top:7px;font-size:13px}}.inline-tariffs{{display:flex;gap:7px;overflow-x:auto;scroll-snap-type:x mandatory;padding-bottom:3px}}.inline-tariffs .tier-group{{flex:0 0 205px;scroll-snap-align:start}}.key-row,.password-form{{grid-template-columns:minmax(0,1fr) auto}}.copy-button{{width:auto;padding:7px 10px}}.app-grid{{grid-template-columns:repeat(4,minmax(0,1fr));gap:5px}}.app-button{{font-size:9px;gap:4px;padding:5px 3px}}.app-icon{{width:14px;height:14px}}.payment-row{{grid-template-columns:42px minmax(48px,1fr) 70px 65px;gap:4px;font-size:10px}}.payment-row time{{grid-column:auto;font-size:9px}}.payment-state{{min-width:66px;padding:3px 5px}}.password-form .button{{width:auto;font-size:10px;padding:7px 9px}}}}@media(max-width:410px){{.renew-heading{{grid-template-columns:1fr 176px}}.renew-heading p{{display:none}}.renew-tab{{font-size:8px}}.payment-method-copy span{{display:none}}.copy-button{{width:44px;padding:0;font-size:0}}.copy-button svg{{width:18px;height:18px}}.app-button{{font-size:8px;display:grid;gap:1px}}.payment-row{{grid-template-columns:34px minmax(45px,1fr) 63px 59px;font-size:9px}}.payment-state{{min-width:58px;font-size:8px}}.password-card>.muted{{display:none}}}}
</style>
<style>
.cabinet-title p,.renew-heading p,.payment-method-copy small,.payment-method-copy span{{color:#5f6b82}}.status-badge{{color:#0f6f3d}}
</style>
<script>const planData={plan_json};const methodData={method_json};function durationLabel(days){{if(!days)return 'срок не указан';if(days%30===0){{const months=days/30;return months+' мес.'}}return days+' дн.'}}function selectedPlan(){{return planData[String(document.getElementById('order-plan').value)]}}function selectedMethod(){{const select=document.getElementById('order-method');const fallback=select?.selectedOptions?.[0];return methodData[select?.value]||{{name:fallback?.textContent||'Способ оплаты',url:fallback?.dataset?.url||'',manual:true}}}}function planTitle(plan){{if(!plan)return 'Тариф не выбран';return plan.name.toLocaleLowerCase().includes(plan.package.toLocaleLowerCase())?plan.name:`${{plan.package}} · ${{plan.name}}`}}function methodAppearance(method){{const value=`${{document.getElementById('order-method')?.value||''}} ${{method?.name||''}}`.toLocaleLowerCase();if(value.includes('крип')||value.includes('crypto'))return{{icon:'₿',hint:'Оплата криптовалютой'}};if(value.includes('мир')||value.includes('card')||value.includes('карт'))return{{icon:'▰',hint:'Оплата банковской картой'}};return{{icon:'▦',hint:'Оплата через приложение банка'}}}}function renderRenewCard(){{const plan=selectedPlan(),method=selectedMethod(),appearance=methodAppearance(method);document.getElementById('payment-method-name').textContent=method.name||'Способ оплаты';document.getElementById('payment-method-hint').textContent=appearance.hint;document.getElementById('payment-method-icon').textContent=appearance.icon;document.getElementById('renew-plan-name').textContent=planTitle(plan);document.getElementById('renew-amount').textContent=plan?`${{plan.price}} ${{plan.currency}}`:'—';document.getElementById('pay-button').textContent=plan?`Оплатить ${{plan.price}} ${{plan.currency}}`:'Выберите тариф'}}function renderPaymentSummary(targetId='payment-summary-modal'){{const target=document.getElementById(targetId);if(!target)return;const plan=selectedPlan(),method=selectedMethod();if(!plan){{target.innerHTML='<p class="error">Выберите подписку</p>';return}}target.innerHTML=`<h3>К оплате</h3><div class="summary-row"><span>Пакет</span><b>${{plan.package}}</b></div><div class="summary-row"><span>Тариф</span><b>${{plan.name}}</b></div><div class="summary-row"><span>Срок</span><b>${{durationLabel(plan.duration_days)}}</b></div><div class="summary-row summary-total"><span>Сумма</span><b>${{plan.price}} ${{plan.currency}}</b></div><div class="summary-row"><span>Способ оплаты</span><b>${{method.name}}</b></div>`}}document.getElementById('order-method')?.addEventListener('change',renderRenewCard);renderRenewCard();function openPayment(){{const out=document.getElementById('order-result');if(out){{out.className='';out.innerHTML=''}}renderPaymentSummary();if(!selectedPlan())return;document.getElementById('payment-modal').classList.add('open')}}function closePayment(){{document.getElementById('payment-modal').classList.remove('open')}}async function copyKey(button){{const key=document.getElementById('vpn-key')?.textContent||'';try{{await navigator.clipboard.writeText(key);button.textContent='Скопировано ✓'}}catch(error){{const range=document.createRange();range.selectNodeContents(document.getElementById('vpn-key'));const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);button.textContent='Ключ выделен'}}}}async function saveCabinetPassword(){{const out=document.getElementById('password-result'),input=document.getElementById('cabinet-password');out.className='';out.textContent='Сохраняем…';const r=await fetch('/web/password',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{password:input.value}})}});const d=await r.json();out.className=r.ok?'success':'error';out.textContent=r.ok?'Пароль сохранён. Теперь можно входить по email и паролю.':(d.detail||'Не удалось сохранить пароль');if(r.ok)input.value=''}}async function confirmPayment(){{await createOrder()}}async function createOrder(){{const out=document.getElementById('order-result');out.className='';out.textContent='Создаём платёж…';const r=await fetch('/web/payments/manual',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{plan_id:Number(document.getElementById('order-plan').value),method_code:document.getElementById('order-method').value}})}});const d=await r.json();if(!r.ok){{out.className='error';out.textContent=d.detail||'Ошибка';return}}if(d.redirect){{if(d.payment_return_token)sessionStorage.setItem('freedom_payment_return_token',d.payment_return_token);out.innerHTML='<p>Платёж создан. Открываем страницу оплаты…</p>';location.href=d.redirect;return}}out.className='';out.innerHTML=d.qr_url?`<p>Оплатите <b>${{d.amount}} ${{d.currency}}</b> выбранным способом, затем загрузите чек.</p><img src="${{d.qr_url}}" alt="QR для оплаты" style="max-width:260px;width:100%"><input id="receipt" type="file" accept="image/png,image/jpeg,image/webp,application/pdf"><button class="button primary" type="button" onclick="uploadReceipt(${{d.payment_id}})">Отправить чек</button>`:`<p>${{d.instructions||'Оплатите по указанным реквизитам и загрузите чек.'}}</p><p><b>Сумма: ${{d.amount}} ${{d.currency}}</b></p><input id="receipt" type="file" accept="image/png,image/jpeg,image/webp,application/pdf"><button class="button primary" type="button" onclick="uploadReceipt(${{d.payment_id}})">Отправить чек</button>`}}async function uploadReceipt(id){{const file=document.getElementById('receipt').files[0];if(!file)return alert('Выберите файл чека');const data=await new Promise(ok=>{{const reader=new FileReader();reader.onload=()=>ok(reader.result.split(',')[1]);reader.readAsDataURL(file)}});const r=await fetch(`/web/payments/${{id}}/receipt`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{filename:file.name,mime_type:file.type,data_base64:data}})}});const d=await r.json();if(r.ok){{alert('Чек отправлен. Платёж ожидает проверки администратора.');location.reload()}}else alert(d.detail||'Ошибка загрузки')}};if({str(checkout).lower()}){{openPayment()}}</script>"""
    body += """<script>
const paymentIcons={
  qr:'<svg class="method-icon icon-qr" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h3v3h-3zM18 14h3v7h-3zM14 18h3v3h-3z"></path><path class="qr-cut" d="M5 5h3v3H5zM16 5h3v3h-3zM5 16h3v3H5z"></path></svg>',
  card:'<svg class="method-icon icon-card" viewBox="0 0 24 24" aria-hidden="true"><rect x="2.5" y="5" width="19" height="14" rx="2.5"></rect><path d="M3 9h18"></path></svg>',
  crypto:'<span class="method-icon icon-crypto" aria-hidden="true">₿</span>'
};
function selectedMethodCode(){return document.querySelector('input[name="payment-method"]:checked')?.value||''}
function selectedMethod(){const code=selectedMethodCode();return methodData[code]||{name:'Способ оплаты',url:'',manual:true}}
function methodAppearance(method){const value=`${selectedMethodCode()} ${method?.name||''}`.toLocaleLowerCase();if(value.includes('крип')||value.includes('crypto'))return{kind:'crypto',hint:'Оплата криптовалютой'};if(value.includes('мир')||value.includes('card')||value.includes('карт'))return{kind:'card',hint:'Оплата банковской картой'};return{kind:'qr',hint:'Оплата через приложение банка'}}
function displayCurrency(currency){return currency==='RUB'?'₽':currency}
function durationLabel(days){if(!days)return'срок не указан';if(days%365===0)return`${days/365} г.`;if(days%30===0)return`${days/30} мес.`;return`${days} дн.`}
function renderRenewCard(){
  const plan=selectedPlan(),method=selectedMethod(),appearance=methodAppearance(method),controls=document.querySelectorAll('input[name="payment-method"]'),picker=document.getElementById('payment-picker'),icon=document.getElementById('payment-method-icon'),pay=document.getElementById('pay-button');
  document.querySelector('.payment-method-control')?.classList.toggle('single-method',controls.length<=1);
  picker?.setAttribute('aria-haspopup',controls.length>1?'true':'false');
  document.getElementById('payment-method-name').textContent=method.name||'Способ оплаты';
  document.getElementById('payment-method-hint').textContent=controls.length===1?'Выбранный способ оплаты':appearance.hint;
  icon.dataset.kind=appearance.kind;icon.innerHTML=paymentIcons[appearance.kind];
  document.getElementById('renew-plan-name').textContent=planTitle(plan);
  document.getElementById('renew-amount').textContent=plan?`${plan.price} ${displayCurrency(plan.currency)}`:'—';
  pay.textContent=plan?`Оплатить ${plan.price} ${displayCurrency(plan.currency)}`:'Выберите тариф';
  pay.disabled=!plan||!selectedMethodCode();
}
function toggleMethodMenu(force){
  const menu=document.getElementById('payment-method-menu'),picker=document.getElementById('payment-picker'),count=document.querySelectorAll('input[name="payment-method"]').length;
  if(!menu||count<=1)return;
  const open=typeof force==='boolean'?force:menu.hidden;
  menu.hidden=!open;picker.setAttribute('aria-expanded',String(open));
  if(open)menu.querySelector('input:checked')?.focus();
}
function setRenewMode(mode){
  const tariffs=mode==='tariffs',card=document.getElementById('payment'),paymentPanel=document.getElementById('payment-panel'),tariffsPanel=document.getElementById('tariffs-panel'),paymentTab=document.getElementById('payment-tab'),tariffsTab=document.getElementById('tariffs-tab');
  card.classList.toggle('tariffs-mode',tariffs);paymentPanel.hidden=tariffs;tariffsPanel.hidden=!tariffs;
  paymentTab.classList.toggle('active',!tariffs);tariffsTab.classList.toggle('active',tariffs);
  paymentTab.setAttribute('aria-selected',String(!tariffs));tariffsTab.setAttribute('aria-selected',String(tariffs));
  paymentTab.tabIndex=tariffs?-1:0;tariffsTab.tabIndex=tariffs?0:-1;
  document.getElementById('renew-subtitle').textContent=tariffs?'Выберите пакет и срок':'Выберите способ оплаты';
  toggleMethodMenu(false);
}
function selectInlinePlan(id,returnToPayment=true){
  document.getElementById('order-plan').value=String(id);
  document.querySelectorAll('.inline-tariffs [data-order-plan]').forEach(button=>button.classList.toggle('selected',button.dataset.orderPlan===String(id)));
  document.querySelectorAll('.inline-tariffs .tier-group').forEach(group=>group.classList.toggle('selected',!!group.querySelector('.duration.selected')));
  renderRenewCard();
  if(returnToPayment)setTimeout(()=>setRenewMode('payment'),180);
}
function renderPaymentSummary(targetId='payment-summary-modal'){
  const target=document.getElementById(targetId),plan=selectedPlan(),method=selectedMethod();if(!target)return;
  if(!plan){target.innerHTML='<p class="error">Выберите подписку</p>';return}
  target.innerHTML=`<h3>К оплате</h3><div class="summary-row"><span>Пакет</span><b>${plan.package}</b></div><div class="summary-row"><span>Тариф</span><b>${plan.name}</b></div><div class="summary-row"><span>Срок</span><b>${durationLabel(plan.duration_days)}</b></div><div class="summary-row summary-total"><span>Сумма</span><b>${plan.price} ${displayCurrency(plan.currency)}</b></div><div class="summary-row"><span>Способ оплаты</span><b>${method.name}</b></div>`;
}
document.querySelectorAll('input[name="payment-method"]').forEach(input=>input.addEventListener('change',()=>{renderRenewCard();toggleMethodMenu(false)}));
document.querySelectorAll('.inline-tariffs [data-order-plan]').forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();selectInlinePlan(button.dataset.orderPlan)}));
document.querySelectorAll('.renew-tab').forEach((tab,index,tabs)=>tab.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const next=tabs[(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length];next.click();next.focus()}));
document.addEventListener('click',event=>{if(!event.target.closest('.payment-method-control'))toggleMethodMenu(false)});
document.addEventListener('keydown',event=>{if(event.key==='Escape')toggleMethodMenu(false)});
function openPayment(){const out=document.getElementById('order-result');if(out){out.className='';out.innerHTML=''}if(!selectedMethodCode()){toggleMethodMenu(true);return}renderPaymentSummary();if(!selectedPlan())return;document.getElementById('payment-modal').classList.add('open')}
async function copyKey(button){
  const key=document.getElementById('vpn-key')?.dataset.value||'',original=button.innerHTML;let copied=false;
  try{await navigator.clipboard.writeText(key);copied=true}catch(error){const textarea=document.createElement('textarea');textarea.value=key;textarea.setAttribute('readonly','');textarea.style.position='fixed';textarea.style.opacity='0';document.body.appendChild(textarea);textarea.focus();textarea.select();textarea.setSelectionRange(0,key.length);copied=document.execCommand('copy');textarea.remove()}
  button.textContent=copied?'Скопировано ✓':'Не удалось скопировать';button.setAttribute('aria-label',button.textContent);setTimeout(()=>{button.innerHTML=original;button.setAttribute('aria-label','Скопировать VPN-ключ')},1500);
}
async function confirmPayment(){await createOrder()}
async function createOrder(){
  const out=document.getElementById('order-result'),confirm=document.querySelector('#payment-modal .actions .primary'),methodCode=selectedMethodCode();if(!methodCode){out.className='error';out.textContent='Выберите способ оплаты';return}
  out.className='';out.textContent='Создаём платёж…';confirm.disabled=true;confirm.classList.add('loading');confirm.textContent='Создаём…';
  try{const r=await fetch('/web/payments/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan_id:Number(document.getElementById('order-plan').value),method_code:methodCode})});const d=await r.json();if(!r.ok){out.className='error';out.textContent=d.detail||'Ошибка';return}if(d.redirect){if(d.payment_return_token)sessionStorage.setItem('freedom_payment_return_token',d.payment_return_token);out.innerHTML='<p>Платёж создан. Открываем страницу оплаты…</p>';location.href=d.redirect;return}out.className='';out.innerHTML=d.qr_url?`<p>Оплатите <b>${d.amount} ${d.currency}</b> выбранным способом, затем загрузите чек.</p><img src="${d.qr_url}" alt="QR для оплаты" style="max-width:260px;width:100%"><input id="receipt" type="file" accept="image/png,image/jpeg,image/webp,application/pdf"><button class="button primary" type="button" onclick="uploadReceipt(${d.payment_id})">Отправить чек</button>`:`<p>${d.instructions||'Оплатите по указанным реквизитам и загрузите чек.'}</p><p><b>Сумма: ${d.amount} ${d.currency}</b></p><input id="receipt" type="file" accept="image/png,image/jpeg,image/webp,application/pdf"><button class="button primary" type="button" onclick="uploadReceipt(${d.payment_id})">Отправить чек</button>`}finally{confirm.disabled=false;confirm.classList.remove('loading');confirm.textContent='Продлить'}
}
selectInlinePlan(document.getElementById('order-plan').value,false);setRenewMode('payment');renderRenewCard();
</script>"""
    await db.commit()
    return HTMLResponse(_shell(body, title="Управление подпиской — Freedom VPN"), headers=_headers())


@router.get("/cabinet/tariffs", response_class=HTMLResponse)
async def cabinet_tariffs(
    cabinet_token: str | None = Cookie(default=None, alias=COOKIE),
    db: AsyncSession = Depends(get_db),
):
    await _require_cabinet(cabinet_token, db)
    plans = await _plans(db)
    packages = await _plan_packages(db)
    packages_by_id = {item.id: item for item in packages}
    groups: dict[str, list[Plan]] = {}
    package_order: list[tuple[str, str, str]] = []
    for package in packages:
        groups.setdefault(package.code, [])
        package_order.append(
            (
                package.code,
                package.name,
                package.description
                or (
                    ("без ограничений" if not package.max_connections else f"{package.max_connections} подключений")
                    + " · "
                    + ("без ограничений" if not package.traffic_limit_gb else f"{package.traffic_limit_gb} ГБ трафика")
                ),
            )
        )
    for plan in plans:
        code = _package_code(plan, packages_by_id)
        if not code:
            continue
        groups.setdefault(code, []).append(plan)
        if code not in {item[0] for item in package_order}:
            label = _package_label(plan, packages_by_id)
            package_order.append((code, label, ""))

    cards = []
    for code, label, description in package_order:
        tier_plans = sorted(groups.get(code, []), key=lambda item: (item.duration_days, item.price))
        if not tier_plans:
            continue
        buttons = "".join(
            f'<a class="duration" href="/cabinet?plan_id={plan.id}#payment">{html.escape(_clean_plan_name(plan.name))} · {plan.price:g} ₽</a>'
            for plan in tier_plans
        )
        cards.append(
            f'<article class="tier-group"><h3>{html.escape(label)}</h3>'
            f'<p class="muted">{html.escape(description)}</p><div class="duration-buttons">{buttons}</div></article>'
        )
    body = f'''<div class="wrap"><nav><a class="brand" href="/"><img src="/static/freedom-vpn-logo-web.webp" alt="">Freedom <i>VPN</i></a><a class="button" href="/cabinet">В кабинет</a></nav><main class="cabinet"><h2>Сменить тариф</h2><p class="muted">Выберите пакет и срок. После выбора вернём вас в кабинет, где можно оплатить продление.</p><div class="tier-groups tariff-page">{''.join(cards) or '<p class="muted">Тарифы временно недоступны.</p>'}</div></main></div><style>.tariff-page .tier-group{{cursor:default}}.tariff-page .duration{{display:inline-flex;text-decoration:none}}</style>'''
    return HTMLResponse(_shell(body, title="Сменить тариф — Freedom VPN"), headers=_headers())


@router.post("/cabinet/logout")
async def cabinet_logout():
    response = RedirectResponse("/cabinet", status_code=303, headers=_headers())
    response.delete_cookie(COOKIE, path="/")
    return response


@router.post("/web/payments/manual")
async def web_manual_payment(data: WebOrder, cabinet_token: str | None = Cookie(default=None, alias=COOKIE), db: AsyncSession = Depends(get_db)):
    user, _ = await _require_cabinet(cabinet_token, db)
    method = await db.scalar(select(PaymentMethod).where(PaymentMethod.code == data.method_code, PaymentMethod.is_active.is_(True)))
    if method is None:
        raise HTTPException(status_code=404, detail="Способ оплаты не найден")
    if method.code in {"sber_qr", "tbank_qr"} and method.image_data is None:
        raise HTTPException(status_code=409, detail=f"QR для способа оплаты «{method.name}» ещё не загружен")
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
        if is_platega_method(method.code):
            return_token = _payment_return_token(user.id)
            base_url = settings.public_base_url.rstrip("/")
            payment = await create_platega_payment(
                db,
                PaymentCreate(user_id=user.id, plan_id=data.plan_id, node_id=node.id, client_type="universal", flow="", fingerprint="firefox", idempotency_key=f"web:{user.id}:{secrets.token_hex(12)}"),
                method_code=method.code,
                source="web_cabinet",
                return_url=f"{base_url}/cabinet/payment-return?payment=success&token={return_token}",
                failed_url=f"{base_url}/cabinet/payment-return?payment=failed&token={return_token}",
            )
            platega = (payment.details or {}).get("platega") or {}
            return {
                "payment_id": payment.id,
                "status": payment.status,
                "amount": str(payment.amount),
                "currency": payment.currency,
                "redirect": platega.get("redirect"),
                "payment_return_token": return_token,
                "instructions": "Откройте ссылку Platega для оплаты. После подтверждения платежа VPN будет выдан автоматически.",
            }
        payment = await create_payment(
            db,
            PaymentCreate(user_id=user.id, plan_id=data.plan_id, node_id=node.id, client_type="universal", flow="", fingerprint="firefox", idempotency_key=f"web:{user.id}:{secrets.token_hex(12)}"),
            provider="manual_bank",
        )
    except PaymentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PlategaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    payment.details = {**(payment.details or {}), "method_code": method.code, "source": "web_cabinet"}
    await db.commit()
    await db.refresh(payment)
    await notify_payment_created(db, payment)
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
    await db.refresh(payment)
    await notify_payment_receipt(db, payment)
    return {"payment_id": payment.id, "status": payment.status}
