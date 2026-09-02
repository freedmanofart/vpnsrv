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
        "Никому не сообщайте этот код. Если вы его не запрашивали, "
        "просто проигнорируйте письмо."
    )
    try:
        await asyncio.to_thread(_send, message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("Не удалось отправить письмо") from exc
