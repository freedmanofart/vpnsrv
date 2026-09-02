import sys
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("SERVICE_API_TOKEN", "test-service-token")

from app.core.config import settings
from app.services import notifications


class AdminNotificationTests(TestCase):
    def test_payment_actions_include_approve_button(self) -> None:
        payment = SimpleNamespace(id=42, status="processing")

        markup = notifications._payment_actions(payment)

        self.assertEqual(
            "admin_pay:paid:42",
            markup["inline_keyboard"][0][0]["callback_data"],
        )
        self.assertEqual("✅ Подтвердить", markup["inline_keyboard"][0][0]["text"])

    def test_support_chat_id_is_derived_from_support_url(self) -> None:
        previous = settings.support_url
        settings.support_url = "https://t.me/Freedom_VPN_Support"
        try:
            self.assertEqual("@Freedom_VPN_Support", notifications._support_chat_id())
        finally:
            settings.support_url = previous
