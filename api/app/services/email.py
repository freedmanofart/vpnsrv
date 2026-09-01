import asyncio
import smtplib
from email.message import EmailMessage

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


async def send_cabinet_link(email: str, link: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise EmailDeliveryError("Отправка почты пока не настроена")
    message = EmailMessage()
    message["Subject"] = "Вход в кабинет Freedom VPN"
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(
        "Ваша резервная ссылка для управления подпиской Freedom VPN:\n\n"
        f"{link}\n\n"
        "Не пересылайте эту ссылку другим людям. Она действует как пароль."
    )
    try:
        await asyncio.to_thread(_send, message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("Не удалось отправить письмо") from exc
