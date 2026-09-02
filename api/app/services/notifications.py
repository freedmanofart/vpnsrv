import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.payment import Payment
from app.db.models.payment_method import PaymentMethod
from app.db.models.plan import Plan
from app.db.models.user import User
from app.db.models.vpn_node import VPNNode
from app.services.email import _send


logger = logging.getLogger(__name__)


async def _payment_card(db: AsyncSession, payment: Payment, *, title: str) -> str:
    user = await db.get(User, payment.user_id)
    plan = await db.get(Plan, payment.plan_id)
    node = await db.get(VPNNode, payment.node_id) if payment.node_id else None
    method_name = "—"
    method_code = (payment.details or {}).get("method_code")
    if method_code:
        method = await db.scalar(
            select(PaymentMethod).where(PaymentMethod.code == method_code).limit(1)
        )
        if method is not None:
            method_name = method.name
        else:
            method_name = str(method_code)

    source = (payment.details or {}).get("source") or payment.provider
    telegram_id = user.telegram_id if user else None
    username = f"@{user.username}" if user and user.username else "—"
    email = user.email if user and user.email else "—"
    plan_name = plan.name if plan else f"plan_id={payment.plan_id}"
    duration = f"{plan.duration_days} дн." if plan else "—"
    node_name = node.region or node.name if node else "—"

    return (
        f"{title}\n\n"
        f"Платёж: #{payment.id}\n"
        f"Статус: {payment.status}\n"
        f"Сумма: {payment.amount:g} {payment.currency}\n"
        f"Тариф: {plan_name} ({duration})\n"
        f"Способ оплаты: {method_name}\n"
        f"Источник: {source}\n"
        f"Нода: {node_name}\n\n"
        f"Пользователь ID: {payment.user_id}\n"
        f"Telegram ID: {telegram_id or '—'}\n"
        f"Username: {username}\n"
        f"Email: {email}\n\n"
        "Проверка: VPN Admin → Платежи"
    )


async def _send_email(subject: str, body: str, *, attachment: tuple[str, str, bytes] | None = None) -> None:
    if not settings.admin_notification_email or not settings.smtp_host or not settings.smtp_from:
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = settings.admin_notification_email
    message.set_content(body)
    if attachment is not None:
        filename, mime_type, content = attachment
        maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
        message.add_attachment(
            content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=filename or "receipt",
        )
    try:
        await asyncio.to_thread(_send, message)
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("admin_email_notification_failed: %s", exc)


async def _send_telegram_message(text: str) -> None:
    if not settings.bot_token or not settings.bot_admin_chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
                json={"chat_id": settings.bot_admin_chat_id, "text": text[:4096]},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("admin_telegram_notification_failed: %s", exc)


async def _send_telegram_receipt(text: str, *, filename: str, mime_type: str, content: bytes) -> None:
    if not settings.bot_token or not settings.bot_admin_chat_id:
        return
    method = "sendPhoto" if (mime_type or "").startswith("image/") else "sendDocument"
    field = "photo" if method == "sendPhoto" else "document"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.bot_token}/{method}",
                data={"chat_id": settings.bot_admin_chat_id, "caption": text[:1024]},
                files={field: (filename or "receipt", content, mime_type or "application/octet-stream")},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("admin_telegram_receipt_failed: %s", exc)


async def notify_payment_created(db: AsyncSession, payment: Payment) -> None:
    card = await _payment_card(db, payment, title="🧾 Новая покупка Freedom VPN")
    await _send_telegram_message(card)
    await _send_email(f"Новая покупка Freedom VPN #{payment.id}", card)


async def notify_payment_receipt(db: AsyncSession, payment: Payment) -> None:
    if not payment.receipt_data:
        return
    card = await _payment_card(db, payment, title="📎 Загружен чек Freedom VPN")
    filename = payment.receipt_filename or f"receipt-{payment.id}"
    mime_type = payment.receipt_mime_type or "application/octet-stream"
    await _send_telegram_receipt(card, filename=filename, mime_type=mime_type, content=payment.receipt_data)
    await _send_email(
        f"Чек по платежу Freedom VPN #{payment.id}",
        card,
        attachment=(filename, mime_type, payment.receipt_data),
    )
