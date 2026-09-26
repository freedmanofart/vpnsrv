import sys
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("SERVICE_API_TOKEN", "test-service-token")

from app.services import notifications


class AdminNotificationTests(TestCase):
    def test_payment_actions_include_approve_button(self) -> None:
        payment = SimpleNamespace(id=42, provider="manual_bank", status="processing")

        markup = notifications._payment_actions(payment)

        self.assertEqual(
            "admin_pay:paid:42",
            markup["inline_keyboard"][0][0]["callback_data"],
        )
        self.assertEqual("✅ Подтвердить", markup["inline_keyboard"][0][0]["text"])

    def test_platega_pending_payment_has_no_approve_button(self) -> None:
        payment = SimpleNamespace(id=42, provider="platega", status="pending")

        markup = notifications._payment_actions(payment)

        self.assertEqual({}, markup)

    def test_support_chat_id_is_not_derived_from_public_links(self) -> None:
        self.assertEqual("@Freedom_VPN_Support", notifications._support_chat_id())


class ProviderCodeNotificationTests(IsolatedAsyncioTestCase):
    async def test_provider_code_is_sent_to_allowlisted_email(self) -> None:
        user = SimpleNamespace(
            id=3,
            email="FreedmanOfArt@icloud.com",
            telegram_id=106123347,
        )
        with (
            patch.object(
                notifications.settings,
                "provider_code_notifications_enabled",
                True,
            ),
            patch.object(
                notifications.settings,
                "provider_code_notification_email",
                "freedmanofart@icloud.com",
            ),
            patch.object(
                notifications,
                "_send_client_telegram_message",
                new=AsyncMock(return_value=True),
            ) as send_message,
            patch.object(
                notifications,
                "_send_client_email_message",
                new=AsyncMock(return_value=True),
            ) as send_email,
            patch.object(notifications, "write_audit", new=AsyncMock()),
        ):
            sent = await notifications.notify_provider_activation_code(
                AsyncMock(),
                user,
                "00123456",
                device_name="dev-a1b2c3",
                ttl_minutes=10,
            )

        self.assertTrue(sent.telegram_sent)
        self.assertTrue(sent.email_sent)
        send_message.assert_awaited_once()
        send_email.assert_awaited_once()
        message = send_message.await_args.args[1]
        self.assertIn("00123456", message)
        self.assertIn("Имя устройства: dev-a1b2c3", message)
        self.assertIn("10 мин.", message)

    async def test_provider_code_is_not_sent_to_other_email(self) -> None:
        user = SimpleNamespace(email="other@example.com", telegram_id=123)
        with (
            patch.object(
                notifications.settings,
                "provider_code_notifications_enabled",
                True,
            ),
            patch.object(
                notifications.settings,
                "provider_code_notification_email",
                "freedmanofart@icloud.com",
            ),
            patch.object(
                notifications,
                "_send_client_telegram_message",
                new=AsyncMock(return_value=True),
            ) as send_message,
            patch.object(
                notifications,
                "_send_client_email_message",
                new=AsyncMock(return_value=True),
            ) as send_email,
        ):
            sent = await notifications.notify_provider_activation_code(
                AsyncMock(),
                user,
                "00123456",
                device_name="dev-a1b2c3",
                ttl_minutes=10,
            )

        self.assertFalse(sent.telegram_sent)
        self.assertFalse(sent.email_sent)
        send_message.assert_not_awaited()
        send_email.assert_not_awaited()


class PaidNotificationTests(IsolatedAsyncioTestCase):
    async def test_paid_client_notifications_include_incy_import_link_and_comment(self) -> None:
        user = SimpleNamespace(
            id=7,
            email="user@example.com",
            telegram_id=123,
        )
        plan = SimpleNamespace(name="Лайт 1 день")
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        subscription = SimpleNamespace(expires_at=expires_at)
        client = SimpleNamespace(id=42, status="active", expires_at=expires_at)
        payment = SimpleNamespace(
            id=9,
            user_id=user.id,
            plan_id=3,
            subscription_id=11,
            amount=10,
            currency="RUB",
        )

        class FakeDB:
            async def get(self, model, key):
                if model is notifications.User:
                    return user
                if model is notifications.Plan:
                    return plan
                if model is notifications.Subscription:
                    return subscription
                return None

            async def scalar(self, query):
                return client

        with (
            patch.object(notifications, "_payment_paid_notification_was_sent", new=AsyncMock(return_value=False)),
            patch.object(notifications, "_payment_card", new=AsyncMock(return_value="admin-card")),
            patch.object(notifications, "_send_telegram_message", new=AsyncMock()),
            patch.object(notifications, "_send_email", new=AsyncMock()),
            patch.object(notifications, "_send_client_telegram_message", new=AsyncMock(return_value=True)) as send_message,
            patch.object(notifications, "_send_client_email_message", new=AsyncMock(return_value=True)) as send_email,
            patch("app.services.provider_codes.issue_provider_code", new=AsyncMock(side_effect=RuntimeError("skip"))),
            patch.object(notifications, "write_audit", new=AsyncMock()),
        ):
            await notifications.notify_payment_paid(FakeDB(), payment)

        telegram_text = send_message.await_args.args[1]
        email_text = send_email.await_args.args[2]
        for text in (telegram_text, email_text):
            self.assertIn("/v1/client/import/", text)
            self.assertIn("Импортировать конфигурацию в INCY", text)
            self.assertIn("Откройте ссылку на устройстве с установленным INCY", text)
            self.assertIn("Из-за блокировок РКН", text)
