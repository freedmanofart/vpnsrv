import sys
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase


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
