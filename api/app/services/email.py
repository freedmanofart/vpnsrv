import asyncio
import smtplib
from email.message import EmailMessage
from urllib.parse import urljoin

from app.core.config import settings


class EmailDeliveryError(RuntimeError):
    pass


def _send(message: EmailMessage) -> None:
    smtp_class = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
    with smtp_class(settings.smtp_host, settings.smtp_port, timeout=15) as client:
        if settings.smtp_starttls and not settings.smtp_use_ssl:
            client.starttls()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


async def send_cabinet_code(email: str, code: str, ttl_minutes: int) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise EmailDeliveryError("Отправка почты пока не настроена")
    message = EmailMessage()
    message["Subject"] = "Вход в кабинет Freedom VPN"
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(
        "Код для входа в кабинет Freedom VPN:\n\n"
        f"{code}\n\n"
        f"Код действует {ttl_minutes} минут и может быть использован один раз.\n"
        "\n"
        f"{_cabinet_renewal_footer()}\n\n"
        "Никому не сообщайте этот код. Если вы его не запрашивали, "
        "просто проигнорируйте письмо."
    )
    try:
        await asyncio.to_thread(_send, message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("Не удалось отправить письмо") from exc


def _cabinet_url() -> str:
    return urljoin(settings.public_base_url.rstrip("/") + "/", "cabinet")


def _renew_url() -> str:
    return _cabinet_url() + "?checkout=1#payment"


def _cabinet_renewal_footer() -> str:
    return (
        f"Web-кабинет: {_cabinet_url()}\n"
        f"Продлить подписку: {_renew_url()}\n\n"
        "Если подписка скоро закончится или уже закончилась, зайдите в web-кабинет "
        "и продлите доступ — после оплаты VPN-ключ обновится автоматически."
    )


async def send_subscription_expiring(
    email: str,
    *,
    plan_name: str,
    expires_at: str,
    days_remaining: int,
) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise EmailDeliveryError("Отправка почты пока не настроена")
    message = EmailMessage()
    message["Subject"] = "Freedom VPN: услуга скоро закончится"
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(
        "Здравствуйте!\n\n"
        "Ваша услуга Freedom VPN скоро закончится.\n\n"
        f"Тариф: {plan_name}\n"
        f"Действует до: {expires_at} UTC\n"
        f"Осталось дней: {days_remaining}\n\n"
        f"{_cabinet_renewal_footer()}"
    )
    try:
        await asyncio.to_thread(_send, message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("Не удалось отправить письмо") from exc


async def send_subscription_expired(
    email: str,
    *,
    plan_name: str,
    expires_at: str,
) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise EmailDeliveryError("Отправка почты пока не настроена")
    message = EmailMessage()
    message["Subject"] = "Freedom VPN: услуга закончилась"
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(
        "Здравствуйте!\n\n"
        "Срок действия вашей услуги Freedom VPN закончился.\n\n"
        f"Тариф: {plan_name}\n"
        f"Закончилась: {expires_at} UTC\n\n"
        f"{_cabinet_renewal_footer()}"
    )
    try:
        await asyncio.to_thread(_send, message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("Не удалось отправить письмо") from exc
