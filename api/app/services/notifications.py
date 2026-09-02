import asyncio
import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.audit import AuditLog
from app.db.models.payment import Payment
from app.db.models.payment_method import PaymentMethod
from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_node import VPNNode
from app.services.admin_settings import get_admin_contacts
from app.services.audit import write_audit
from app.services.email import (
    EmailDeliveryError,
    _send,
    send_subscription_expired,
    send_subscription_expiring,
)


logger = logging.getLogger(__name__)


def _support_chat_id() -> str:
    value = (settings.support_url or "").strip()
    if not value:
        return "@Freedom_VPN_Support"
    if value.startswith("@"):
        return value
    parsed = urlparse(value)
    if parsed.netloc.lower() in {"t.me", "telegram.me"}:
        username = parsed.path.strip("/").split("/", 1)[0]
        if username:
            return f"@{username}"
    return value


async def _telegram_destinations(db: AsyncSession) -> list[int | str]:
    contacts = await get_admin_contacts(db)
    destinations: list[int | str] = []
    if contacts.bot_admin_chat_id:
        destinations.append(contacts.bot_admin_chat_id)
    support = _support_chat_id()
    if support and support not in destinations:
        destinations.append(support)
    return destinations


def _payment_actions(payment: Payment) -> dict:
    buttons = []
    if payment.status in {"pending", "processing"}:
        buttons.append(
            [
                {"text": "✅ Подтвердить", "callback_data": f"admin_pay:paid:{payment.id}"},
                {"text": "❌ Ошибка", "callback_data": f"admin_pay:failed:{payment.id}"},
            ]
        )
        buttons.append(
            [{"text": "🚫 Отменить", "callback_data": f"admin_pay:cancelled:{payment.id}"}]
        )
    elif payment.status == "paid":
        buttons.append(
            [{"text": "↩️ Возврат", "callback_data": f"admin_pay:refunded:{payment.id}"}]
        )
    return {"inline_keyboard": buttons} if buttons else {}


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


async def _send_email(
    db: AsyncSession,
    subject: str,
    body: str,
    *,
    attachment: tuple[str, str, bytes] | None = None,
) -> None:
    contacts = await get_admin_contacts(db)
    if not contacts.admin_notification_email or not settings.smtp_host or not settings.smtp_from:
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = contacts.admin_notification_email
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


async def _send_telegram_message(
    db: AsyncSession,
    text: str,
    *,
    reply_markup: dict | None = None,
) -> None:
    if not settings.bot_token:
        return
    destinations = await _telegram_destinations(db)
    if not destinations:
        return
    async with httpx.AsyncClient(timeout=15.0) as client:
        for chat_id in destinations:
            try:
                payload = {"chat_id": chat_id, "text": text[:4096]}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                response = await client.post(
                    f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                details = ""
                if isinstance(exc, httpx.HTTPStatusError):
                    details = exc.response.text[:500]
                logger.warning("admin_telegram_notification_failed chat_id=%s: %s %s", chat_id, exc, details)


async def _send_telegram_receipt(
    db: AsyncSession,
    text: str,
    *,
    filename: str,
    mime_type: str,
    content: bytes,
    reply_markup: dict | None = None,
) -> None:
    if not settings.bot_token:
        return
    destinations = await _telegram_destinations(db)
    if not destinations:
        return
    method = "sendPhoto" if (mime_type or "").startswith("image/") else "sendDocument"
    field = "photo" if method == "sendPhoto" else "document"
    async with httpx.AsyncClient(timeout=30.0) as client:
        for chat_id in destinations:
            try:
                data = {"chat_id": chat_id, "caption": text[:1024]}
                if reply_markup:
                    data["reply_markup"] = json.dumps(reply_markup)
                response = await client.post(
                    f"https://api.telegram.org/bot{settings.bot_token}/{method}",
                    data=data,
                    files={field: (filename or "receipt", content, mime_type or "application/octet-stream")},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                details = ""
                if isinstance(exc, httpx.HTTPStatusError):
                    details = exc.response.text[:500]
                logger.warning("admin_telegram_receipt_failed chat_id=%s: %s %s", chat_id, exc, details)


async def notify_payment_created(db: AsyncSession, payment: Payment) -> None:
    card = await _payment_card(db, payment, title="🧾 Новая покупка Freedom VPN")
    await _send_telegram_message(db, card, reply_markup=_payment_actions(payment))
    await _send_email(db, f"Новая покупка Freedom VPN #{payment.id}", card)


async def notify_payment_receipt(db: AsyncSession, payment: Payment) -> None:
    if not payment.receipt_data:
        return
    card = await _payment_card(db, payment, title="📎 Загружен чек Freedom VPN")
    filename = payment.receipt_filename or f"receipt-{payment.id}"
    mime_type = payment.receipt_mime_type or "application/octet-stream"
    await _send_telegram_receipt(
        db,
        card,
        filename=filename,
        mime_type=mime_type,
        content=payment.receipt_data,
        reply_markup=_payment_actions(payment),
    )
    await _send_email(
        db,
        f"Чек по платежу Freedom VPN #{payment.id}",
        card,
        attachment=(filename, mime_type, payment.receipt_data),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _notification_was_sent(
    db: AsyncSession,
    *,
    action: str,
    subscription_id: int,
) -> bool:
    return bool(
        await db.scalar(
            select(
                exists().where(
                    AuditLog.action == action,
                    AuditLog.resource_type == "subscription",
                    AuditLog.resource_id == str(subscription_id),
                    AuditLog.result == "success",
                )
            )
        )
    )


async def _notification_was_recorded(
    db: AsyncSession,
    *,
    action: str,
    subscription_id: int,
) -> bool:
    return bool(
        await db.scalar(
            select(
                exists().where(
                    AuditLog.action == action,
                    AuditLog.resource_type == "subscription",
                    AuditLog.resource_id == str(subscription_id),
                )
            )
        )
    )


async def notify_subscription_email_reminders(db: AsyncSession) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    reminder_until = now + timedelta(days=settings.subscription_expiration_reminder_days)
    sent_expiring = 0
    sent_expired = 0
    failed = 0
    skipped = 0

    expiring_rows = await db.execute(
        select(Subscription, User, Plan)
        .join(User, User.id == Subscription.user_id)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.status == "active",
            Subscription.expires_at > now,
            Subscription.expires_at <= reminder_until,
        )
    )
    expired_rows = await db.execute(
        select(Subscription, User, Plan)
        .join(User, User.id == Subscription.user_id)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            or_(
                Subscription.status == "expired",
                (
                    (Subscription.status == "active")
                    & (Subscription.expires_at <= now)
                ),
            ),
        )
    )

    for subscription, user, plan in expiring_rows:
        expires_at = _aware(subscription.expires_at)
        if not user.email:
            if not await _notification_was_recorded(
                db,
                action="email.subscription_expiring.skip",
                subscription_id=subscription.id,
            ):
                skipped += 1
                await write_audit(
                    db,
                    action="email.subscription_expiring.skip",
                    result="skipped",
                    resource_type="subscription",
                    resource_id=subscription.id,
                    details={
                        "reason": "user_email_missing",
                        "user_id": user.id,
                        "telegram_id": user.telegram_id,
                        "expires_at": expires_at.isoformat(),
                    },
                    commit=False,
                )
            continue
        if await _notification_was_sent(
            db,
            action="email.subscription_expiring.send",
            subscription_id=subscription.id,
        ):
            continue
        days_remaining = max(1, int(((expires_at - now).total_seconds() + 86399) // 86400))
        try:
            await send_subscription_expiring(
                user.email,
                plan_name=plan.name,
                expires_at=expires_at.strftime("%Y-%m-%d %H:%M"),
                days_remaining=days_remaining,
            )
        except EmailDeliveryError as exc:
            failed += 1
            logger.warning("subscription_expiring_email_failed: %s", exc)
            await write_audit(
                db,
                action="email.subscription_expiring.send",
                result="failure",
                resource_type="subscription",
                resource_id=subscription.id,
                details={
                    "email": user.email,
                    "error": str(exc),
                    "expires_at": expires_at.isoformat(),
                    "user_id": user.id,
                },
                commit=False,
            )
            continue
        sent_expiring += 1
        await write_audit(
            db,
            action="email.subscription_expiring.send",
            result="success",
            resource_type="subscription",
            resource_id=subscription.id,
            details={
                "email": user.email,
                "expires_at": expires_at.isoformat(),
                "days_remaining": days_remaining,
                "user_id": user.id,
            },
            commit=False,
        )

    for subscription, user, plan in expired_rows:
        expires_at = _aware(subscription.expires_at)
        if not user.email:
            if not await _notification_was_recorded(
                db,
                action="email.subscription_expired.skip",
                subscription_id=subscription.id,
            ):
                skipped += 1
                await write_audit(
                    db,
                    action="email.subscription_expired.skip",
                    result="skipped",
                    resource_type="subscription",
                    resource_id=subscription.id,
                    details={
                        "reason": "user_email_missing",
                        "user_id": user.id,
                        "telegram_id": user.telegram_id,
                        "expires_at": expires_at.isoformat(),
                    },
                    commit=False,
                )
            continue
        if await _notification_was_sent(
            db,
            action="email.subscription_expired.send",
            subscription_id=subscription.id,
        ):
            continue
        try:
            await send_subscription_expired(
                user.email,
                plan_name=plan.name,
                expires_at=expires_at.strftime("%Y-%m-%d %H:%M"),
            )
        except EmailDeliveryError as exc:
            failed += 1
            logger.warning("subscription_expired_email_failed: %s", exc)
            await write_audit(
                db,
                action="email.subscription_expired.send",
                result="failure",
                resource_type="subscription",
                resource_id=subscription.id,
                details={
                    "email": user.email,
                    "error": str(exc),
                    "expires_at": expires_at.isoformat(),
                    "user_id": user.id,
                },
                commit=False,
            )
            continue
        sent_expired += 1
        await write_audit(
            db,
            action="email.subscription_expired.send",
            result="success",
            resource_type="subscription",
            resource_id=subscription.id,
            details={
                "email": user.email,
                "expires_at": expires_at.isoformat(),
                "user_id": user.id,
            },
            commit=False,
        )

    if sent_expiring or sent_expired or failed or skipped:
        await db.commit()

    return {
        "subscription_expiring_emails": sent_expiring,
        "subscription_expired_emails": sent_expired,
        "subscription_email_failures": failed,
        "subscription_email_skipped": skipped,
    }
