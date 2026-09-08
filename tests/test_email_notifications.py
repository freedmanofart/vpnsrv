import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.core.config import settings
from app.services import email as email_service


class EmailNotificationContentTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_public_base_url = settings.public_base_url
        self.previous_smtp_host = settings.smtp_host
        self.previous_smtp_from = settings.smtp_from
        settings.public_base_url = "https://vpn.example.test"
        settings.smtp_host = "smtp.example.test"
        settings.smtp_from = "Freedom VPN <noreply@example.test>"

    async def asyncTearDown(self) -> None:
        settings.public_base_url = self.previous_public_base_url
        settings.smtp_host = self.previous_smtp_host
        settings.smtp_from = self.previous_smtp_from

    async def test_all_client_emails_include_cabinet_renewal_link(self) -> None:
        sent = []

        def collect(message):
            sent.append(message)

        with patch.object(email_service, "_send", side_effect=collect):
            await email_service.send_cabinet_code("user@example.test", "123456", 10)
            await email_service.send_subscription_expiring(
                "user@example.test",
                plan_name="1 месяц",
                expires_at="2026-09-10 12:00:00",
                days_remaining=3,
            )
            await email_service.send_subscription_expired(
                "user@example.test",
                plan_name="1 месяц",
                expires_at="2026-09-01 12:00:00",
            )

        self.assertEqual(3, len(sent))
        for message in sent:
            body = message.get_content()
            self.assertIn("Web-кабинет: https://vpn.example.test/cabinet", body)
            self.assertIn(
                "Продлить подписку: https://vpn.example.test/cabinet?checkout=1#payment",
                body,
            )
            self.assertIn("продлите доступ", body)
