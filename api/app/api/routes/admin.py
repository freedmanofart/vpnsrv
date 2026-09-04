from datetime import datetime, timedelta, timezone
import asyncio
import html
import logging
import os
from pathlib import Path
import re
import smtplib
import subprocess
import time
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from fastapi.responses import HTMLResponse, Response as FastAPIResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.models.payment import Payment
from app.db.models.payment_method import PaymentMethod
from app.db.models.audit import AccessGrant, AuditLog, ClientDevice, DebugSession
from app.db.models.cabinet_login_code import CabinetLoginCode
from app.db.session import get_db
from app.core.security import APIPrincipal, hash_password, require_admin, require_api_access
from app.services.audit import write_audit
from app.services.admin_settings import get_admin_contacts, set_admin_contacts
from app.services.node_health import node_accepts_clients
from app.services.vless import build_vless_url
from app.services.threexui import ThreeXUIClient, ThreeXUIError, ThreeXUIClientNotFound
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


class AdminUserPasswordSet(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class AdminContactsUpdate(BaseModel):
    admin_notification_email: str = Field(max_length=320)
    bot_admin_chat_id: int = 0


logger = logging.getLogger(__name__)

HOST_COMMANDS = {
    "backup_postgres": "scripts/backup.sh",
    "verify_latest_backup": "latest=$(ls -t /var/backups/vpn-service/vpn-db-*.dump 2>/dev/null | head -1) && scripts/verify_backup.sh \"$latest\"",
    "restore_postgres": "scripts/restore_postgres.sh /var/backups/vpn-service/vpn-db-TIMESTAMP.dump",
    "mail_chain_recover": "scripts/check_mail_chain.sh",
    "online_apis": "scripts/check_online_apis.sh",
    "platega_check": "docker cp scripts/check_platega_payment.py vpn-api:/tmp/check_platega_payment.py && docker exec vpn-api python /tmp/check_platega_payment.py --method all --amount 10 --currency RUB --description \"Freedom VPN Platega admin check\"",
    "tailscale_recover": "scripts/recover_tailscale.sh",
    "tailscale_funnel": "tailscale funnel status",
}

BACKUP_DIR = Path(os.getenv("VPN_BACKUP_DIR", "/var/backups/vpn-service"))


def _repo_root() -> Path:
    env_root = os.getenv("VPN_REPO_ROOT")
    candidates = []
    if env_root:
        candidates.append(Path(env_root).resolve())
    candidates.extend(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "docs").is_dir() and (candidate / "alembic.ini").is_file():
            return candidate
    return Path(__file__).resolve().parents[4]


REPO_ROOT = _repo_root()


def _safe_database_target() -> str:
    match = re.match(r"^[^:]+://(?:[^:@/]+(?::[^@/]*)?@)?([^/]+)/(.+)$", settings.database_url)
    if not match:
        return "DATABASE_URL настроен; значение скрыто"
    return f"{match.group(1)}/{match.group(2).split('?', 1)[0]}"


def _resource_rows(xui_links: list[str]) -> list[dict]:
    base = settings.public_base_url.rstrip("/")
    return [
        {"name": "Публичный сайт", "url": settings.public_base_url},
        {"name": "Web-кабинет", "url": f"{base}/cabinet"},
        {"name": "VPN Admin", "url": f"{base}/admin", "note": "HTTP Basic; логин и пароль задаются ADMIN_USERNAME/ADMIN_PASSWORD"},
        {"name": "API health", "url": f"{base}/health"},
        {"name": "Swagger UI", "url": f"{base}/docs", "note": "По документации сейчас публичен в FastAPI; закрывайте на reverse proxy, если нужно"},
        {"name": "OpenAPI schema", "url": f"{base}/openapi.json"},
        {"name": "DB health", "url": f"{base}/db-health", "note": "Требует авторизацию"},
        {"name": "Local API на сервере", "url": "http://127.0.0.1:8000"},
        {"name": "PostgreSQL", "target": _safe_database_target(), "note": "Общий контейнер postgres; VPN использует отдельную БД vpn"},
        {"name": "PostgreSQL backups", "path": str(BACKUP_DIR)},
        {"name": "Production repo", "path": "/home/freedman/vpn-service"},
        {"name": "Tailscale Funnel", "command": "tailscale funnel status"},
        {"name": "Tailscale certificate timer", "command": "systemctl status vpn-tailscale-cert.timer --no-pager"},
        {"name": "PostgreSQL backup timer", "command": "systemctl status vpn-backup.timer --no-pager"},
        {
            "name": "3x-ui master SSH forward",
            "command": "ssh -L 2222:127.0.0.1:41026 root@freedomvpn",
            "url": "http://localhost:2222/<private-3x-ui-base-path>/panel/clients",
            "note": "Откройте URL после запуска SSH-туннеля на операторской машине",
        },
        *[
            {"name": f"VPN API / Master 3x-ui #{index + 1}", "url": url, "note": "Адрес из vpn_node_configs.api_address"}
            for index, url in enumerate(xui_links)
        ],
    ]


ADMIN_DOCS = [
    {"id": "frontend", "name": "Frontend-структура сайта и web-кабинета", "path": "docs/frontend-site-structure.md"},
    {"id": "web_cabinet", "name": "Web-кабинет, почта, коды и пароли", "path": "docs/web-cabinet.md"},
    {"id": "notifications", "name": "Уведомления: email, bot, lifecycle", "path": "docs/notifications.md"},
    {"id": "platega", "name": "Подключение Platega: бот и web-кабинет", "path": "docs/platega-bot-payment-integration.md"},
    {"id": "vpn_lifecycle", "name": "Жизненный цикл VPN API", "path": "docs/vpn-api-lifecycle.md"},
    {"id": "latest_changes", "name": "Последние изменения", "path": "docs/latest-changes-2026-09-01.md"},
    {"id": "access", "name": "Доступы и переменные", "path": "docs/access-and-credentials.md"},
    {"id": "bot", "name": "Редактирование Telegram-бота", "path": "docs/editing-telegram-bot.md"},
    {"id": "maintenance", "name": "Скрипты обслуживания", "path": "docs/maintenance-scripts.md"},
    {"id": "xui_master", "name": "3x-ui master и SSH proxy", "path": "docs/3x-ui-master.md"},
    {"id": "add_node", "name": "Добавление 3x-ui ноды", "path": "docs/add-3x-ui-node.md"},
    {"id": "admin_3xui", "name": "Администрирование 3x-ui API", "path": "docs/admin-3xui-api.md"},
    {"id": "remediation", "name": "План восстановления и исправлений", "path": "docs/remediation-plan.md"},
]


async def _public_probe(path: str = "/", timeout: float = 5.0) -> dict:
    url = f"{settings.public_base_url.rstrip('/')}{path}"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
        return {
            "name": f"Сайт {path}",
            "status": "online" if response.status_code < 500 else "degraded",
            "details": f"HTTP {response.status_code}",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except httpx.HTTPError as exc:
        return {
            "name": f"Сайт {path}",
            "status": "offline",
            "details": str(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


def _check_smtp() -> dict:
    host = settings.smtp_host
    if not host:
        return {"name": "Email SMTP", "status": "offline", "details": "SMTP_HOST не задан"}
    port = int(settings.smtp_port or 587)
    started = time.perf_counter()
    try:
        smtp_class = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
        with smtp_class(host, port, timeout=8) as smtp:
            smtp.ehlo()
            if settings.smtp_starttls and not settings.smtp_use_ssl:
                smtp.starttls()
                smtp.ehlo()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
        return {
            "name": "Email SMTP",
            "status": "online",
            "details": f"{host}:{port}, login ok",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except (OSError, smtplib.SMTPException) as exc:
        return {
            "name": "Email SMTP",
            "status": "offline",
            "details": str(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


async def _check_nodes(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(VPNNode, VPNNodeConfig)
        .join(VPNNodeConfig, VPNNodeConfig.node_id == VPNNode.id)
        .where(VPNNode.status != "disabled", VPNNodeConfig.protocol == "vless")
    )
    checks = []
    for node, config in result.all():
        api_address = config.config.get("api_address")
        inbound_tag = config.config.get("inbound_tag", "vless-reality")
        started = time.perf_counter()
        if not api_address:
            checks.append({"name": f"VPN-нода #{node.id} {node.name}", "status": "offline", "details": "api_address не задан"})
            continue
        try:
            users = await ThreeXUIClient(address=api_address, timeout=5.0).get_users(inbound_tag)
            node.health_status = "online"
            node.latency_ms = round((time.perf_counter() - started) * 1000, 2)
            node.last_seen_at = datetime.now(timezone.utc)
            checks.append({
                "name": f"VPN-нода #{node.id} {node.name}",
                "status": "online",
                "details": f"3x-ui ok, inbound {inbound_tag}, users={len(users)}",
                "latency_ms": node.latency_ms,
            })
        except ThreeXUIError as exc:
            node.health_status = "offline"
            node.latency_ms = round((time.perf_counter() - started) * 1000, 2)
            node.last_seen_at = datetime.now(timezone.utc)
            checks.append({
                "name": f"VPN-нода #{node.id} {node.name}",
                "status": "offline",
                "details": str(exc),
                "latency_ms": node.latency_ms,
            })
    await db.commit()
    return checks


def _host_script_result(script_id: str) -> dict:
    command = HOST_COMMANDS.get(script_id)
    if not command:
        raise HTTPException(status_code=404, detail="Unknown admin script")
    return {
        "script_id": script_id,
        "status": "host_required",
        "message": "Эта операция должна выполняться на SSH-хосте, а не внутри API-контейнера.",
        "command": f"cd /home/freedman/vpn-service && {command}",
    }


def _run_command(command: list[str], timeout: int = 120) -> dict:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "failed",
            "output": str(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return {
        "status": "done" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "output": output[-6000:] if output else "",
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _backup_files() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    rows = []
    for item in sorted(BACKUP_DIR.glob("vpn-db-*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        stat = item.stat()
        rows.append(
            {
                "name": item.name,
                "path": str(item),
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return rows


def _pg_url() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _doc_html(markdown: str) -> str:
    body = html.escape(markdown)
    body = re.sub(r"^# (.+)$", r"<h1>\1</h1>", body, flags=re.MULTILINE)
    body = re.sub(r"^## (.+)$", r"<h2>\1</h2>", body, flags=re.MULTILINE)
    body = re.sub(r"^### (.+)$", r"<h3>\1</h3>", body, flags=re.MULTILINE)
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Документация</title><style>body{{max-width:980px;margin:0 auto;padding:28px;font:16px/1.6 system-ui,sans-serif;color:#111827}}pre{{white-space:pre-wrap;background:#f5f7fb;border:1px solid #e6eaf2;border-radius:12px;padding:16px}}code{{background:#f5f7fb;padding:2px 5px;border-radius:6px}}a{{color:#175cff}}</style></head><body><a href="/admin">← Админка</a><pre>{body}</pre></body></html>"""


def _create_postgres_backup() -> dict:
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump_path = BACKUP_DIR / f"vpn-db-{timestamp}.dump"
    result = _run_command(["pg_dump", _pg_url(), "-Fc", "-f", str(dump_path)], timeout=180)
    if result["status"] == "done":
        dump_path.chmod(0o600)
        verify = _run_command(["pg_restore", "--list", str(dump_path)], timeout=60)
        result["dump"] = str(dump_path)
        result["verify"] = verify
        result["backups"] = _backup_files()
    return result


def _verify_latest_postgres_backup() -> dict:
    backups = _backup_files()
    if not backups:
        return {"status": "failed", "output": f"Backup-файлы не найдены в {BACKUP_DIR}"}
    latest = backups[0]["path"]
    result = _run_command(["pg_restore", "--list", latest], timeout=60)
    result["dump"] = latest
    result["backups"] = backups
    return result


async def _mail_chain_recover(db: AsyncSession) -> dict:
    checks = [
        {"name": "API контейнер", "status": "online", "details": "/admin отвечает"},
    ]
    try:
        await db.execute(text("SELECT 1"))
        checks.append({"name": "PostgreSQL", "status": "online", "details": "SELECT 1 ok"})
    except Exception as exc:  # pragma: no cover - admin diagnostics
        checks.append({"name": "PostgreSQL", "status": "offline", "details": str(exc)})
    try:
        await db.execute(text("ALTER TABLE cabinet_login_codes ADD COLUMN IF NOT EXISTS plain_code VARCHAR(6)"))
        await db.commit()
        checks.append({"name": "Колонка кодов", "status": "online", "details": "cabinet_login_codes.plain_code ok"})
    except Exception as exc:  # pragma: no cover - admin diagnostics
        await db.rollback()
        checks.append({"name": "Колонка кодов", "status": "offline", "details": str(exc)})
    checks.append(await asyncio.to_thread(_check_smtp))
    recent_result = await db.execute(
        select(CabinetLoginCode).order_by(CabinetLoginCode.id.desc()).limit(10)
    )
    now = datetime.now(timezone.utc)
    recent_codes = []
    for code in recent_result.scalars().all():
        expires_at = _aware(code.expires_at)
        recent_codes.append(
            {
                "id": code.id,
                "user_id": code.user_id,
                "code": code.plain_code or "legacy_hash_only",
                "status": "used" if code.used_at else ("expired" if expires_at <= now else "active"),
                "attempts": code.attempts,
                "created_at": code.created_at,
                "expires_at": code.expires_at,
                "used_at": code.used_at,
            }
        )
    return {
        "status": "online" if all(item["status"] == "online" for item in checks) else "attention",
        "checks": checks,
        "recent_codes": recent_codes,
        "restart_command": "cd /home/freedman/vpn-service && docker compose up -d --force-recreate api bot",
    }


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


@router.get("/docs/{doc_id}", response_class=HTMLResponse, include_in_schema=False)
async def admin_doc(doc_id: str, _: APIPrincipal = Depends(require_admin)):
    doc = next((item for item in ADMIN_DOCS if item["id"] == doc_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    path = (REPO_ROOT / doc["path"]).resolve()
    if not path.is_file() or REPO_ROOT not in path.parents:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return HTMLResponse(_doc_html(path.read_text(encoding="utf-8")))


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
    login_codes = await rows(CabinetLoginCode)
    audit_result = await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(300))
    audit_logs = audit_result.scalars().all()
    paid_trial_result = await db.execute(
        select(AccessGrant)
        .where(AccessGrant.kind == "paid_trial", AccessGrant.code == "PAID_TRIAL_3H")
        .order_by(AccessGrant.id.desc())
    )
    paid_trial_grants = paid_trial_result.scalars().all()
    email_result = await db.execute(
        select(AuditLog)
        .where(AuditLog.action.like("email.%"))
        .order_by(AuditLog.id.desc())
        .limit(200)
    )
    email_logs = email_result.scalars().all()
    config_result = await db.execute(
        select(VPNNodeConfig).where(VPNNodeConfig.protocol == "vless")
    )
    configs = config_result.scalars().all()
    admin_contacts = await get_admin_contacts(db)
    plan_map = {item.id: item for item in plans}
    node_map = {item.id: item for item in nodes}
    config_map = {item.node_id: item for item in configs}
    xui_links = sorted(
        {
            str(config.config.get("api_address"))
            for config in configs
            if config.config.get("api_address")
        }
    )
    subscription_map = {}
    for item in subscriptions:
        if item.status in {"active", "disabled"}:
            subscription_map.setdefault(item.user_id, item)
    client_map = {}
    for item in clients:
        if item.status in {"active", "disabled"}:
            client_map.setdefault(item.subscription_id, item)
    paid_trial_map = {}
    for grant in paid_trial_grants:
        paid_trial_map.setdefault(grant.user_id, grant)

    user_rows = []
    for user in users:
        subscription = subscription_map.get(user.id)
        plan = plan_map.get(subscription.plan_id) if subscription else None
        client = client_map.get(subscription.id) if subscription else None
        node = node_map.get(client.node_id) if client else None
        config = config_map.get(client.node_id) if client else None
        link = _client_link(client, node, config) if client and node and config else None
        paid_trial = paid_trial_map.get(user.id)
        paid_trial_active = bool(
            paid_trial
            and subscription
            and paid_trial.subscription_id == subscription.id
            and subscription.status == "active"
            and _aware(subscription.expires_at) > datetime.now(timezone.utc)
        )
        user_rows.append(
            {
                "id": user.id,
                "telegram_id": user.telegram_id,
                "username": user.username,
                "account_status": user.status,
                "email": user.email,
                "password": "set" if user.password_hash else "not_set",
                "paid_trial": (
                    f"активен до {paid_trial_active and subscription.expires_at or ''}"
                    if paid_trial_active
                    else ("использован" if paid_trial else "не использован")
                ),
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
        "admin_contacts": {
            "admin_notification_email": admin_contacts.admin_notification_email,
            "bot_admin_chat_id": admin_contacts.bot_admin_chat_id,
        },
        "users": user_rows,
        "plans": [{"id": x.id, "code": x.code, "name": x.name, "duration_days": x.duration_days, "max_connections": x.max_connections, "traffic_limit_gb": x.traffic_limit_gb, "price": str(x.price), "currency": x.currency, "active": x.is_active, "public": x.is_public} for x in plans],
        "nodes": [{"id": x.id, "name": x.name, "provider": x.provider, "region": x.region, "ip": x.ip_address, "status": x.status, "health": x.health_status, "latency_ms": x.latency_ms, "last_seen_at": x.last_seen_at, "capacity": x.capacity, "connections": x.active_connections} for x in nodes if x.status != "disabled"],
        "subscriptions": [{"id": x.id, "user_id": x.user_id, "plan_id": x.plan_id, "status": x.status, "expires_at": x.expires_at} for x in subscriptions],
        "clients": [{"id": x.id, "user_id": x.user_id, "subscription_id": x.subscription_id, "node_id": x.node_id, "client_type": x.client_type, "flow": x.flow, "max_connections": x.max_connections, "status": x.status, "expires_at": x.expires_at, "last_connected_at": x.last_connected_at, "last_ip": x.last_ip, "link_overridden": bool(x.config_override)} for x in clients],
        "payments": [{"id": x.id, "user_id": x.user_id, "provider": x.provider, "amount": str(x.amount), "currency": x.currency, "status": x.status, "subscription_id": x.subscription_id, "has_receipt": x.receipt_data is not None, "receipt_url": f"/admin/payments/{x.id}/receipt" if x.receipt_data is not None else None, "receipt_filename": x.receipt_filename, "receipt_mime_type": x.receipt_mime_type, "details": x.details, "created_at": x.created_at} for x in payments],
        "payment_methods": [{"id": x.id, "code": x.code, "name": x.name, "url": x.url, "sort_order": x.sort_order, "is_active": x.is_active, "has_image": x.image_data is not None} for x in sorted(payment_methods, key=lambda item: (item.sort_order, item.id))],
        "devices": [{"id": x.id, "user_id": x.user_id, "name": x.name, "platform": x.platform, "status": x.status, "last_seen_at": x.last_seen_at, "expires_at": x.expires_at} for x in devices],
        "login_codes": [{"id": x.id, "user_id": x.user_id, "code": x.plain_code or "legacy_hash_only", "status": "used" if x.used_at else ("expired" if _aware(x.expires_at) <= datetime.now(timezone.utc) else "active"), "attempts": x.attempts, "created_at": x.created_at, "expires_at": x.expires_at, "used_at": x.used_at} for x in login_codes],
        "docs": ADMIN_DOCS,
        "scripts": [
            {"id": "health_dashboard", "name": "Health-dashboard: API, сайт, почта, 3x-ui", "command": "выполняется из админки", "runnable": True},
            {"id": "smtp_check", "name": "Проверить SMTP-логин и отправку писем", "command": "выполняется из админки", "runnable": True},
            {"id": "online_apis", "name": "Все online-тесты ключевых API", "command": "scripts/check_online_apis.sh", "runnable": True},
            {"id": "platega_check", "name": "Проверить Platega: СБП, МИР, крипта", "command": "scripts/check_platega_payment.py --method all --amount 10 --currency RUB", "runnable": True},
            {"id": "mail_chain_recover", "name": "Проверить почту и быстро переподнять api/bot", "command": "scripts/check_mail_chain.sh", "runnable": True},
            {"id": "tailscale_recover", "name": "Быстро переподнять Tailscale, Funnel и сертификат", "command": "scripts/recover_tailscale.sh", "runnable": True},
            {"id": "backup_postgres", "name": "Создать PostgreSQL backup сейчас", "command": "pg_dump -Fc в /var/backups/vpn-service", "runnable": True},
            {"id": "verify_latest_backup", "name": "Проверить последний PostgreSQL backup", "command": "pg_restore --list для последнего dump", "runnable": True},
            {"id": "list_backups", "name": "Показать последние PostgreSQL backup-файлы", "command": "ls -lh /var/backups/vpn-service/vpn-db-*.dump", "runnable": True},
            {"id": "restore_postgres", "name": "Восстановить PostgreSQL из backup", "command": "scripts/restore_postgres.sh /var/backups/vpn-service/vpn-db-TIMESTAMP.dump", "runnable": True},
            {"id": "tailscale_funnel", "name": "Проверить Tailscale Funnel", "command": "tailscale funnel status", "runnable": True},
            {"name": "Обновить Tailscale certificate", "command": "systemctl start vpn-tailscale-cert.service", "runnable": False},
            {"name": "Посмотреть последние backup-файлы", "command": "ls -lh /var/backups/vpn-service | tail -20", "runnable": False},
            {"name": "Проверить пользователя Telegram", "command": "scripts/check_online_apis.sh TELEGRAM_ID", "runnable": False},
            {"name": "Проверить последние ошибки API/бота", "command": "docker compose logs --tail=200 api bot worker | grep -i -E \"error|failed|exception|traceback|503\" || true", "runnable": False},
        ],
        "resources": _resource_rows(xui_links),
        "email_logs": [
            {
                "id": x.id,
                "created_at": x.created_at,
                "action": x.action,
                "subscription_id": x.resource_id if x.resource_type == "subscription" else None,
                "user_id": (x.details or {}).get("user_id") or (x.resource_id if x.resource_type == "user" else None),
                "telegram_id": (x.details or {}).get("telegram_id"),
                "email": (x.details or {}).get("email"),
                "result": x.result,
                "reason": (x.details or {}).get("reason"),
                "event": (x.details or {}).get("event_type"),
                "error": (x.details or {}).get("error"),
                "expires_at": (x.details or {}).get("expires_at"),
                "days_remaining": (x.details or {}).get("days_remaining"),
            }
            for x in email_logs
        ],
        "debug": [{"id": x.id, "created_by": x.created_by, "reason": x.reason, "status": x.status, "expires_at": x.expires_at} for x in debug_sessions],
        "audit": [{"id": x.id, "created_at": x.created_at, "actor": f"{x.actor_type}:{x.actor_id or '-'}", "action": x.action, "resource": f"{x.resource_type or '-'}:{x.resource_id or '-'}", "result": x.result, "node_id": x.node_id, "sensitive": x.sensitive, "details": x.details} for x in audit_logs],
    }


@router.get("/health-dashboard", dependencies=[Depends(require_admin)])
async def health_dashboard(db: AsyncSession = Depends(get_db)):
    checks = [
        {"name": "API контейнер", "status": "online", "details": "/admin отвечает"},
    ]
    try:
        await db.execute(text("SELECT 1"))
        checks.append({"name": "PostgreSQL", "status": "online", "details": "SELECT 1 ok"})
    except Exception as exc:  # pragma: no cover - admin diagnostics
        checks.append({"name": "PostgreSQL", "status": "offline", "details": str(exc)})
    public_results = await asyncio.gather(
        _public_probe("/"),
        _public_probe("/cabinet"),
        _public_probe("/admin"),
        _public_probe("/plans"),
        _public_probe("/payment-methods"),
    )
    checks.extend(public_results)
    checks.append(await asyncio.to_thread(_check_smtp))
    checks.extend(await _check_nodes(db))
    return {
        "generated_at": datetime.now(timezone.utc),
        "status": "online" if all(item["status"] == "online" for item in checks) else "attention",
        "checks": checks,
    }


@router.post("/scripts/{script_id}/run", dependencies=[Depends(require_admin)])
async def run_admin_script(script_id: str, db: AsyncSession = Depends(get_db)):
    if script_id in {"health_dashboard", "online_apis"}:
        return await health_dashboard(db)
    if script_id == "smtp_check":
        return {"status": "done", "checks": [await asyncio.to_thread(_check_smtp)]}
    if script_id == "mail_chain_recover":
        return await _mail_chain_recover(db)
    if script_id == "backup_postgres":
        return await asyncio.to_thread(_create_postgres_backup)
    if script_id == "verify_latest_backup":
        return await asyncio.to_thread(_verify_latest_postgres_backup)
    if script_id == "list_backups":
        return {"status": "done", "backups": _backup_files()}
    return _host_script_result(script_id)


@router.put("/settings/admin-contacts", dependencies=[Depends(require_admin)])
async def update_admin_contacts(
    data: AdminContactsUpdate,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    email_address = data.admin_notification_email.strip()
    if email_address and not re.fullmatch(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email_address):
        raise HTTPException(status_code=422, detail="Укажите корректный email")
    contacts = await set_admin_contacts(
        db,
        admin_notification_email=email_address,
        bot_admin_chat_id=data.bot_admin_chat_id,
    )
    await write_audit(
        db,
        action="admin.contacts.update",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="settings",
        resource_id="admin_contacts",
        details={
            "admin_notification_email": contacts.admin_notification_email,
            "bot_admin_chat_id": contacts.bot_admin_chat_id,
        },
    )
    return {
        "admin_notification_email": contacts.admin_notification_email,
        "bot_admin_chat_id": contacts.bot_admin_chat_id,
    }


@router.get("/settings/admin-contacts/service", dependencies=[Depends(require_api_access)])
async def get_admin_contacts_for_service(db: AsyncSession = Depends(get_db)):
    contacts = await get_admin_contacts(db)
    return {
        "admin_notification_email": contacts.admin_notification_email,
        "bot_admin_chat_id": contacts.bot_admin_chat_id,
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


@router.post("/users/{user_id}/password", dependencies=[Depends(require_admin)])
async def set_user_password(
    user_id: int,
    data: AdminUserPasswordSet,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(data.password)
    await db.commit()
    await write_audit(
        db,
        action="user.password.set",
        result="success",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="user",
        resource_id=user.id,
        details={"password": "changed_manually"},
    )
    return {"user_id": user.id, "password": "set"}


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


@router.delete("/users/{user_id}/paid-trial", dependencies=[Depends(require_admin)])
async def reset_user_paid_trial(
    user_id: int,
    principal: APIPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    grant_result = await db.execute(
        select(AccessGrant)
        .where(
            AccessGrant.user_id == user_id,
            AccessGrant.kind == "paid_trial",
            AccessGrant.code == "PAID_TRIAL_3H",
        )
        .order_by(AccessGrant.id.desc())
        .limit(1)
    )
    grant = grant_result.scalar_one_or_none()
    if grant is None:
        return {"user_id": user.id, "reset": False, "detail": "Paid trial was not used"}

    grant_id = grant.id
    grant_subscription_id = grant.subscription_id
    now = datetime.now(timezone.utc)
    revoked_clients = 0
    removed_from_panel = 0
    panel_errors: list[str] = []
    if grant_subscription_id:
        subscription = await db.get(Subscription, grant_subscription_id)
        if subscription and subscription.status in {"active", "disabled"}:
            subscription.status = "expired"
            subscription.expires_at = min(_aware(subscription.expires_at), now)
        client_result = await db.execute(
            select(VPNClient).where(VPNClient.subscription_id == grant_subscription_id)
        )
        for client in client_result.scalars():
            if client.status in {"active", "disabled"}:
                config_result = await db.execute(
                    select(VPNNodeConfig).where(
                        VPNNodeConfig.node_id == client.node_id,
                        VPNNodeConfig.protocol == client.protocol,
                    )
                )
                node_config = config_result.scalar_one_or_none()
                if client.protocol == "vless" and node_config and node_config.config.get("api_address"):
                    try:
                        await ThreeXUIClient(address=node_config.config.get("api_address")).remove_vless_user(
                            inbound_tag=node_config.config.get("inbound_tag", "vless-reality"),
                            email=f"vpn-{client.id}",
                        )
                        removed_from_panel += 1
                    except ThreeXUIClientNotFound:
                        pass
                    except ThreeXUIError as exc:
                        panel_errors.append(str(exc))
                client.status = "revoked"
                client.revoked_at = now
                revoked_clients += 1
            client.config_override = None

    await db.delete(grant)
    await db.commit()
    await write_audit(
        db,
        action="user.paid_trial.reset",
        result="success" if not panel_errors else "partial",
        actor_type="admin",
        actor_id=principal.name,
        resource_type="user",
        resource_id=user.id,
        details={
            "grant_id": grant_id,
            "subscription_id": grant_subscription_id,
            "revoked_clients": revoked_clients,
            "removed_from_panel": removed_from_panel,
            "panel_errors": panel_errors,
        },
    )
    return {
        "user_id": user.id,
        "reset": True,
        "revoked_clients": revoked_clients,
        "removed_from_panel": removed_from_panel,
        "panel_errors": panel_errors,
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
    if payment.provider == "platega" and data.status == "paid":
        raise HTTPException(
            status_code=409,
            detail="Platega payments are confirmed automatically by webhook",
        )
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
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(payment.receipt_filename or 'receipt')}"},
    )


ADMIN_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VPN Admin</title><style>
:root{color-scheme:dark;--bg:#0b1020;--card:#141b2d;--line:#29334d;--accent:#61dafb;--bad:#ff6b6b;--ok:#55d187;--warn:#ffd166}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf2ff;font:14px system-ui,sans-serif}.layout{min-height:100vh;display:grid;grid-template-columns:280px 1fr}.sidebar{position:sticky;top:0;height:100vh;padding:22px 18px;border-right:1px solid var(--line);background:#0f1729;overflow:auto}.sidebar h1{font-size:24px;margin:0 0 8px}.sidebar-actions{display:grid;gap:10px;margin:18px 0}.content{min-width:0;padding:24px 3vw;display:grid;gap:22px;align-content:start}.card,section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;min-width:0}nav{display:grid;gap:8px}button,.action{background:#263454;color:white;border:1px solid #3c4c73;border-radius:8px;padding:9px 13px;cursor:pointer;text-decoration:none;display:inline-block;white-space:nowrap}button:hover,.action:hover,button.active{border-color:var(--accent)}button.active{background:#1b3a61}button.danger{color:#ffb1b1}input,select,textarea{width:100%;background:#0d1425;color:white;border:1px solid var(--line);border-radius:8px;padding:9px}form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}form button{align-self:end}.table{overflow:auto;border:1px solid var(--line);border-radius:12px;background:#0d1425}table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap;overflow-wrap:normal;word-break:normal}th{position:sticky;top:0;z-index:1;background:#101a2e;color:#9fb0d0;font-size:12px;text-transform:uppercase;letter-spacing:.04em}.filter-row th{top:38px;background:#0d1425;text-transform:none;letter-spacing:0}.filter-row input{min-width:120px;padding:6px 8px;font-size:12px}td{max-width:340px}.cell-clip{display:inline-block;max-width:300px;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom}.cell-long{white-space:normal;word-break:break-word;overflow-wrap:anywhere}details.cell-long{max-width:420px}summary{cursor:pointer;color:var(--accent)}.badge{display:inline-block;border:1px solid #3c4c73;border-radius:999px;padding:3px 8px;background:#17233a}.badge.ok{border-color:#2d8f66;background:#113528}.badge.bad{border-color:#9a3d45;background:#3a1720}.hidden{display:none}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}code{color:#b9e9ff}.muted{color:#9fb0d0}#notice{min-height:20px}dialog{width:min(850px,94vw);background:var(--card);color:#edf2ff;border:1px solid var(--line);border-radius:14px;padding:20px}dialog::backdrop{background:#000a}.wide{grid-column:1/-1}.dialog-actions{display:flex;gap:10px;justify-content:flex-end;grid-column:1/-1}.script-list,.resource-list,.doc-list,.health-grid{display:grid;gap:10px}.health-grid{grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}.script-item,.resource-item,.doc-item,.health-item{border:1px solid var(--line);border-radius:12px;padding:12px;background:#0d1425}.health-item.ok{border-color:#2d8f66}.health-item.bad{border-color:#9a3d45}.health-item.warn{border-color:#8a7230}.script-item code,.script-output{display:block;margin-top:7px;white-space:pre-wrap}.script-output{padding:10px;border-radius:10px;background:#080d18;color:#cfe7ff;overflow:auto;max-height:260px}@media(max-width:900px){.layout{grid-template-columns:1fr}.sidebar{position:static;height:auto}.content{padding:16px}.sidebar-actions{grid-template-columns:1fr 1fr}nav{grid-template-columns:1fr 1fr}}</style></head>
<body><div class="layout"><aside class="sidebar"><h1>VPN Admin</h1><div id="notice">Загрузка…</div><div class="sidebar-actions"><button onclick="load()">Обновить</button><a class="action" href="/" target="_blank" rel="noopener">Открыть сайт</a></div><nav id="nav"></nav></aside><main class="content">
<section id="plans"><h2>Тарифы</h2><form onsubmit="createPlan(event)"><input name="code" placeholder="Код" required><input name="name" placeholder="Название" required><input name="duration_days" type="number" placeholder="Дней" required><input name="max_connections" type="number" min="0" max="100" value="5" title="0 — без ограничений" required><input name="traffic_limit_gb" type="number" min="0" value="250" title="0 — без ограничений" required><input name="price" type="number" step="0.01" placeholder="Цена" required><input name="currency" value="RUB" required><button>Создать тариф</button></form><p>Лимиты: количество одновременных IP и трафик в ГБ; 0 снимает соответствующее ограничение.</p><div class="table"></div></section>
<section id="nodes" class="hidden"><h2>VPN-ноды</h2><form onsubmit="createNode(event)"><input name="name" placeholder="Имя" required><input name="provider" placeholder="Провайдер" required><input name="ip_address" placeholder="Публичный IP" required><input name="hostname" placeholder="Hostname"><input name="capacity" type="number" value="100"><button>Создать ноду</button></form><p>Страна определяется автоматически по публичному IP.</p><details><summary>Привязать inbound из 3x-ui master</summary><form onsubmit="createConfig(event)"><input name="node_id" type="number" placeholder="Node ID в VPN Admin" required><input name="api_address" placeholder="https://master.example/base-path" required><input name="host" placeholder="Публичный адрес ноды" required><input name="port" type="number" value="443" required><input name="sni" placeholder="Reality SNI" required><input name="fingerprint" placeholder="Fingerprint" value="chrome" required><input name="pbk" placeholder="Reality public key" required><input name="sid" placeholder="Reality short ID" required><input name="inbound_tag" type="number" min="1" placeholder="3x-ui inbound ID" required><button>Привязать</button></form></details><div class="table"></div></section>
<section id="users" class="hidden"><h2>Пользователи</h2><div class="table"></div></section>
<section id="subscriptions" class="hidden"><h2>Подписки</h2><div class="table"></div></section>
<section id="clients" class="hidden"><h2>VPN-клиенты</h2><div class="table"></div></section>
<section id="payments" class="hidden"><h2>Платежи</h2><div class="table"></div></section>
<section id="payment_methods" class="hidden"><h2>Способы оплаты</h2><form onsubmit="createPaymentMethod(event)"><input name="code" placeholder="Код: sber_qr" required><input name="name" placeholder="Название кнопки" required><input name="url" placeholder="Ссылка на QR или реквизиты/телефон"><input name="sort_order" type="number" value="100" required><button>Добавить способ</button></form><p>Для Сбербанка, Т-Банка и перевода по телефону поле URL используется как ссылка на QR или текст реквизитов. Порядок и активность управляют кнопками Telegram.</p><div class="table"></div></section>
<section id="devices" class="hidden"><h2>Устройства</h2><div class="table"></div></section>
<section id="login_codes" class="hidden"><h2>Коды входа в web-кабинет</h2><p class="bad">Коды дают доступ к аккаунту до истечения срока. Не пересылайте их пользователям в открытых чатах без проверки владельца.</p><div class="table"></div></section>
<section id="email_logs" class="hidden"><h2>Email-логи</h2><p class="muted">Последние почтовые уведомления: коды входа, рассылки по подпискам, ошибки SMTP и пропуски без email.</p><div class="table"></div></section>
<section id="settings" class="hidden"><h2>Настройки</h2><p class="muted">Админские контакты для уведомлений о покупках, чеках и важных событиях.</p><form id="adminContactsForm" onsubmit="saveAdminContacts(event)"><label>Email администратора<input name="admin_notification_email" type="email" placeholder="admin@example.com"></label><label>Telegram chat ID администратора<input name="bot_admin_chat_id" type="number" step="1" placeholder="0"></label><button>Сохранить контакты</button></form><p id="settings-result"></p></section>
<section id="docs" class="hidden"><h2>Документация</h2><p class="muted">Единая точка входа в основные инструкции проекта. Пути открываются на сервере из каталога репозитория.</p><div class="doc-list"></div></section>
<section id="health" class="hidden"><h2>Health-dashboard</h2><p class="muted">Проверка API, публичного Tailscale URL, PostgreSQL, SMTP и VPN API / 3x-ui нод.</p><button onclick="loadHealth()">Проверить сейчас</button><div class="health-grid" id="health-grid"></div></section>
<section id="scripts" class="hidden"><h2>Запуск ключевых скриптов</h2><p class="muted">Кнопки запускают безопасные проверки из админки. Host-only операции для PostgreSQL backup/restore возвращают точную команду для SSH-хоста.</p><div class="script-list"></div></section>
<section id="resources" class="hidden"><h2>Ресурсы инфраструктуры</h2><p class="muted">Быстрые ссылки на сайт, кабинет, админку и master 3x-ui из конфигурации VPN-нод.</p><div class="resource-list"></div></section>
<section id="debug" class="hidden"><h2>Sensitive debug</h2><p class="bad">Активная сессия разрешает сохранять полные ключи в audit log. После диагностики секреты нужно ротировать.</p><form onsubmit="createDebug(event)"><input name="reason" placeholder="Причина" required><input name="duration_minutes" type="number" min="1" max="1440" value="30" required><button>Открыть сессию</button></form><div class="table"></div></section>
<section id="audit" class="hidden"><h2>Audit log</h2><div class="table"></div></section>
</main></div>
<dialog id="accessDialog"><h2>Управление доступом</h2><form id="accessForm" onsubmit="saveAccess(event)"><input name="user_id" type="hidden"><label>Тариф<select name="plan_id" required></select></label><label>VPN-нода<select name="node_id" required></select></label><label>Статус<select name="active"><option value="true">Активен</option><option value="false">Не активен</option></select></label><label>Действует до<input name="expires_at" type="datetime-local" required></label><label class="wide">Выданная VPN-ссылка<textarea name="vpn_link" rows="6" placeholder="Пусто — генерировать автоматически"></textarea></label><div class="wide" id="activityInfo"></div><div class="dialog-actions"><button type="button" class="danger" onclick="resetAccess()">Сбросить план и ссылку</button><button type="button" onclick="document.getElementById('accessDialog').close()">Отмена</button><button>Сохранить</button></div></form></dialog><script>
let state={};const sections=['health','plans','nodes','users','subscriptions','clients','payments','payment_methods','devices','login_codes','email_logs','settings','docs','scripts','resources','debug','audit'];
const labels={health:'Health',plans:'Тарифы',nodes:'VPN-ноды',users:'Пользователи',subscriptions:'Подписки',clients:'VPN-клиенты',payments:'Платежи',payment_methods:'Способы оплаты',devices:'Устройства',login_codes:'Коды входа',email_logs:'Email-логи',settings:'Настройки',docs:'Документация',scripts:'Скрипты',resources:'Инфраструктура',debug:'Debug',audit:'Audit log'};
const columnLabels={id:'ID',telegram_id:'Telegram ID',username:'Username',email:'Email',account_status:'Аккаунт',password:'Пароль',paid_trial:'Пробный доступ',access_active:'Доступ',subscription_id:'Подписка ID',plan_id:'Тариф ID',plan:'Тариф',expires_at:'Действует до',client_id:'Клиент ID',node_id:'Нода ID',client_type:'Тип клиента',flow:'Flow',vpn_link:'VPN-ключ',link_overridden:'Ручная ссылка',last_connected_at:'Последнее подключение',last_ip:'Последний IP',code:'Код',name:'Название',duration_days:'Дней',max_connections:'Подключений',traffic_limit_gb:'Трафик ГБ',price:'Цена',currency:'Валюта',active:'Активен',public:'Публичный',provider:'Провайдер',region:'Регион',ip:'IP',status:'Статус',health:'Health',latency_ms:'Задержка мс',last_seen_at:'Последний сигнал',capacity:'Ёмкость',connections:'Подключения',user_id:'Пользователь ID',amount:'Сумма',subscription_id:'Подписка ID',has_receipt:'Чек',receipt_url:'Ссылка на чек',receipt_filename:'Файл чека',receipt_mime_type:'Тип чека',details:'Детали',created_at:'Создано',url:'URL/реквизиты',sort_order:'Порядок',is_active:'Активен',has_image:'QR',platform:'Платформа',attempts:'Попытки',used_at:'Использован',actor:'Кто',action:'Действие',resource:'Ресурс',result:'Результат',sensitive:'Sensitive'};
function esc(v){if(v!==null&&typeof v==='object')v=JSON.stringify(v);return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function show(id){sections.forEach(x=>document.getElementById(x).classList.toggle('hidden',x!==id));document.querySelectorAll('#nav button').forEach(b=>b.classList.toggle('active',b.dataset.section===id))}
function prettyDate(v){if(!v)return '';let d=new Date(v);return Number.isNaN(d.getTime())?String(v):d.toLocaleString('ru-RU',{year:'2-digit',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}
function formatCell(k,v){if(v===true)return '<span class="badge ok">да</span>';if(v===false)return '<span class="badge bad">нет</span>';if(v===null||v===undefined||v==='')return '<span class="muted">—</span>';if(k.endsWith('_at')||k==='expires_at')return esc(prettyDate(v));if(typeof v==='object'){let s=JSON.stringify(v,null,2);return `<details class="cell-long"><summary>показать</summary><code>${esc(s)}</code></details>`}let s=String(v);if(s.length>42)return `<span class="cell-clip" title="${esc(s)}">${esc(s)}</span>`;return esc(s)}
function rawCell(v){if(v!==null&&typeof v==='object')return JSON.stringify(v);return String(v??'')}
function applyTableFilters(tableEl){let filters=[...tableEl.querySelectorAll('[data-filter-key]')].map(i=>({key:i.dataset.filterKey,value:i.value.trim().toLowerCase()}));tableEl.querySelectorAll('tbody tr').forEach(row=>{let ok=filters.every(f=>!f.value||String(row.getAttribute('data-filter-'+f.key)||'').toLowerCase().includes(f.value));row.style.display=ok?'':'none'})}
function table(id,rows,actions){let keys=rows.length?Object.keys(rows[0]):[],box=document.querySelector('#'+id+' .table');box.innerHTML=rows.length?`<table><thead><tr>${keys.map(k=>`<th>${esc(columnLabels[k]||k)}</th>`).join('')}<th>Действия</th></tr><tr class="filter-row">${keys.map(k=>`<th><input data-filter-key="${esc(k)}" placeholder="фильтр"></th>`).join('')}<th><button type="button" onclick="clearTableFilters(this)">Сброс</button></th></tr></thead><tbody>${rows.map(r=>`<tr ${keys.map(k=>`data-filter-${esc(k)}="${esc(rawCell(r[k]))}"`).join(' ')}>${keys.map(k=>`<td>${formatCell(k,r[k])}</td>`).join('')}<td>${actions?actions(r):''}</td></tr>`).join('')}</tbody></table>`:'Нет данных';box.querySelectorAll('[data-filter-key]').forEach(i=>i.addEventListener('input',()=>applyTableFilters(i.closest('table'))))}
function clearTableFilters(btn){let tableEl=btn.closest('table');tableEl.querySelectorAll('[data-filter-key]').forEach(i=>i.value='');applyTableFilters(tableEl)}
async function request(url,opt={}){let r=await fetch(url,opt);if(!r.ok)throw new Error((await r.text())||r.status);return r.status===204?null:r.json()}
function paymentActions(p){let receipt=p.receipt_url?`<a class="action" href="${esc(p.receipt_url)}" target="_blank" rel="noopener">Открыть чек</a> `:'';if(p.provider==='platega'&&['pending','processing'].includes(p.status))return receipt+'<span class="muted">Ожидает callback Platega</span>';if(['pending','processing'].includes(p.status))return receipt+`<button onclick="setPaymentStatus(${p.id},'paid')">Подтвердить</button> <button class="danger" onclick="setPaymentStatus(${p.id},'failed')">Ошибка</button> <button class="danger" onclick="setPaymentStatus(${p.id},'cancelled')">Отменить</button>`;if(p.status==='paid')return receipt+`<button class="danger" onclick="setPaymentStatus(${p.id},'refunded')">Возврат</button>`;return receipt}
async function load(){try{state=await request('/admin/overview');document.getElementById('notice').innerHTML='<span class="ok">API работает</span>';table('plans',state.plans,r=>`<button onclick="editPlan(${r.id})">Изменить</button> <button class="danger" onclick="deletePlan(${r.id})">Удалить</button>`);table('nodes',state.nodes,r=>`<button onclick="health(${r.id})">Health</button> <button onclick="reconcile(${r.id})">Reconcile</button> <button onclick="editNode(${r.id})">Изменить</button>`);table('users',state.users.map(r=>({...r,vpn_link:r.vpn_link?'выдана':'—'})),r=>`<button onclick="openAccess(${r.id})">Доступ</button> <button onclick="setUserPassword(${r.id})">Пароль</button> ${r.access_active?`<button onclick="rotateUser(${r.id})">Перевыпустить</button>`:''} ${r.paid_trial&&r.paid_trial!=='не использован'?`<button class="danger" onclick="resetPaidTrial(${r.id})">Сбросить пробник</button>`:''}`);table('subscriptions',state.subscriptions,r=>`<button onclick="renew(${r.id})">Продлить</button>`);table('clients',state.clients,r=>r.status==='active'?`<button class="danger" onclick="revoke(${r.id})">Отозвать</button>`:'');table('payments',state.payments,p=>paymentActions(p));table('payment_methods',state.payment_methods,r=>`<button onclick="editPaymentMethod(${r.id})">Изменить</button> <button onclick="choosePaymentImage(${r.id})">${r.has_image?'Заменить QR':'Загрузить QR'}</button> ${r.has_image?`<button class="danger" onclick="deletePaymentImage(${r.id})">Удалить QR</button>`:''} <button class="danger" onclick="deletePaymentMethod(${r.id})">Удалить</button>`);table('devices',state.devices,r=>r.status==='active'?`<button class="danger" onclick="revokeDevice(${r.id})">Отозвать</button>`:'');table('login_codes',state.login_codes);table('email_logs',state.email_logs);renderAdminContacts();renderDocs();renderScripts();renderResources();table('debug',state.debug,r=>r.status==='active'?`<button class="danger" onclick="closeDebug(${r.id})">Закрыть</button>`:'');table('audit',state.audit);}catch(e){document.getElementById('notice').innerHTML='<span class="bad">'+esc(e.message)+'</span>'}}
document.getElementById('nav').innerHTML=sections.map(x=>`<button data-section="${x}" onclick="show('${x}')">${labels[x]}</button>`).join('');
function renderAdminContacts(){let f=document.getElementById('adminContactsForm'),c=state.admin_contacts||{};if(!f)return;f.admin_notification_email.value=c.admin_notification_email||'';f.bot_admin_chat_id.value=c.bot_admin_chat_id||0}
async function saveAdminContacts(e){e.preventDefault();let f=Object.fromEntries(new FormData(e.target));let out=document.getElementById('settings-result');out.className='';out.textContent='Сохраняю…';try{state.admin_contacts=await request('/admin/settings/admin-contacts',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({admin_notification_email:f.admin_notification_email||'',bot_admin_chat_id:Number(f.bot_admin_chat_id||0)})});renderAdminContacts();out.className='ok';out.textContent='Контакты сохранены'}catch(err){out.className='bad';out.textContent=err.message}}
function renderDocs(){document.querySelector('#docs .doc-list').innerHTML=(state.docs||[]).map(d=>`<div class="doc-item"><b>${esc(d.name)}</b><br><code>${esc(d.path)}</code><p><a class="action" href="/admin/docs/${esc(d.id)}" target="_blank" rel="noopener">Открыть</a></p></div>`).join('')||'Нет данных'}
function renderScripts(){document.querySelector('#scripts .script-list').innerHTML=(state.scripts||[]).map(s=>`<div class="script-item"><b>${esc(s.name)}</b><code>${esc(s.command)}</code>${s.runnable&&s.id?`<p><button onclick="runScript('${esc(s.id)}',this)">Запустить</button></p><pre class="script-output" id="script-output-${esc(s.id)}"></pre>`:''}</div>`).join('')||'Нет данных'}
function renderResources(){document.querySelector('#resources .resource-list').innerHTML=(state.resources||[]).map(r=>{let link=r.url?`<br><a class="action" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.url)}</a>`:'';let target=r.target?`<br><code>${esc(r.target)}</code>`:'';let path=r.path?`<br><code>${esc(r.path)}</code>`:'';let command=r.command?`<br><code>${esc(r.command)}</code>`:'';let note=r.note?`<p class="muted">${esc(r.note)}</p>`:'';return `<div class="resource-item"><b>${esc(r.name)}</b>${link}${target}${path}${command}${note}</div>`}).join('')||'Нет данных'}
function renderHealth(data){let box=document.getElementById('health-grid');box.innerHTML=(data.checks||[]).map(h=>{let cls=h.status==='online'?'ok':(h.status==='offline'?'bad':'warn');return `<div class="health-item ${cls}"><b>${esc(h.name)}</b><p class="${cls}">${esc(h.status)}</p><p>${esc(h.details||'')}</p>${h.latency_ms?`<p class="muted">${esc(h.latency_ms)} мс</p>`:''}</div>`}).join('')||'Нет данных'}
async function loadHealth(){try{let data=await request('/admin/health-dashboard');renderHealth(data);document.getElementById('notice').innerHTML=data.status==='online'?'<span class="ok">Health OK</span>':'<span class="warn">Health требует внимания</span>'}catch(e){document.getElementById('notice').innerHTML='<span class="bad">'+esc(e.message)+'</span>'}}
async function runScript(id,btn){let out=document.getElementById('script-output-'+id);btn.disabled=true;out.textContent='Выполняю…';try{let data=await request('/admin/scripts/'+id+'/run',{method:'POST'});out.textContent=JSON.stringify(data,null,2);if(data.checks)renderHealth(data)}catch(e){out.textContent=e.message}finally{btn.disabled=false}}
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
async function setUserPassword(id){let password=prompt('Новый пароль для пользователя #'+id+' (минимум 8 символов)');if(password===null)return;if(password.length<8){alert('Минимум 8 символов');return}if(!confirm('Сменить пароль пользователя #'+id+'? Старый пароль перестанет работать.'))return;try{await request('/admin/users/'+id+'/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});await load();alert('Пароль обновлён')}catch(err){alert(err.message)}}
async function resetAccess(){let id=Number(document.getElementById('accessForm').user_id.value);if(!confirm('Сбросить тариф, отключить доступ и удалить ручную ссылку?'))return;try{await request('/admin/users/'+id+'/access',{method:'DELETE'});document.getElementById('accessDialog').close();await load()}catch(err){alert(err.message)}}
async function resetPaidTrial(id){if(!confirm('Сбросить пробный доступ пользователя #'+id+'?\n\nЭто удалит отметку использования пробника. Если активен 3-часовой пробник — он будет отозван.'))return;try{let r=await request('/admin/users/'+id+'/paid-trial',{method:'DELETE'});await load();alert(r.reset?'Пробный доступ сброшен':'Пробный доступ не был использован')}catch(err){alert(err.message)}}
async function createDebug(e){return sendForm(e,()=>'/admin/debug-sessions',f=>({...f,duration_minutes:Number(f.duration_minutes)}))}
async function closeDebug(id){if(confirm('Закрыть debug-сессию #'+id+'?')){await request('/admin/debug-sessions/'+id,{method:'DELETE'});await load()}}
load();</script></body></html>"""
