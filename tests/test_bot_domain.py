import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from bot.app.content import load_content
from bot.app.domain import (
    PLAN_TIERS,
    country_label,
    rotation_payload,
    plan_tier,
    plans_by_tier,
    select_public_plans,
    subscription_payload,
    supports_threexui,
)


class CountryTests(unittest.TestCase):
    def test_country_code_and_detected_name(self):
        self.assertEqual(country_label("US|США"), "🇺🇸 США")
        self.assertEqual(country_label("NL|Нидерланды"), "🇳🇱 Нидерланды")
        self.assertEqual(country_label("de"), "🇩🇪 DE")

    def test_any_detected_country_is_supported(self):
        self.assertEqual(country_label("FR|Франция"), "🇫🇷 Франция")


class KeyTests(unittest.TestCase):
    def test_purchase_payload_has_one_key_variant(self):
        self.assertEqual(
            subscription_payload(1, 2, 3),
            {
                "user_id": 1,
                "plan_id": 2,
                "node_id": 3,
                "client_type": "universal",
                "flow": "",
                "fingerprint": "firefox",
            },
        )


class PlanSelectionTests(unittest.TestCase):
    def test_empty_allowlist_shows_new_api_plans(self):
        plans = [{"code": "old"}, {"code": "new-three-devices"}]
        self.assertEqual(plans, select_public_plans(plans, ()))

    def test_explicit_allowlist_filters_and_orders(self):
        plans = [{"code": "one"}, {"code": "three"}, {"code": "unlimited"}]
        self.assertEqual(
            [{"code": "unlimited"}, {"code": "one"}],
            select_public_plans(plans, ("unlimited", "one")),
        )

    def test_plans_are_grouped_by_connection_tier(self):
        plans = [
            {"code": "lite_1m", "max_connections": 5},
            {"code": "standard_1m", "max_connections": 15},
            {"code": "ultra_1m", "max_connections": 30},
        ]
        grouped = plans_by_tier(plans)
        self.assertEqual(["lite", "standard", "ultra"], list(grouped))
        self.assertEqual("standard", plan_tier(plans[1]))
        self.assertEqual(30, PLAN_TIERS["ultra"]["connections"])

    def test_rotation_payload_has_one_key_variant(self):
        self.assertEqual(
            rotation_payload(7),
            {
                "node_id": 7,
                "client_type": "universal",
                "flow": "",
                "fingerprint": "firefox",
            },
        )

    def test_only_http_master_and_numeric_inbound_are_eligible(self):
        self.assertTrue(
            supports_threexui(
                [
                    {
                        "protocol": "vless",
                        "config": {
                            "api_address": "http://master.internal/prefix",
                            "inbound_tag": "3",
                        },
                    }
                ]
            )
        )
        self.assertFalse(
            supports_threexui(
                [
                    {
                        "protocol": "vless",
                        "config": {
                            "api_address": "127.0.0.1:10085",
                            "inbound_tag": "vless-reality",
                        },
                    }
                ]
            )
        )


class ContentTests(unittest.TestCase):
    def test_content_file_expands_environment_links(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "content.json"
            path.write_text('{"links":{"support":"${TEST_SUPPORT_URL}"}}')
            previous = os.environ.get("TEST_SUPPORT_URL")
            os.environ["TEST_SUPPORT_URL"] = "https://example.test/support"
            try:
                self.assertEqual(
                    "https://example.test/support",
                    load_content(path)["links"]["support"],
                )
            finally:
                if previous is None:
                    os.environ.pop("TEST_SUPPORT_URL", None)
                else:
                    os.environ["TEST_SUPPORT_URL"] = previous


if __name__ == "__main__":
    unittest.main()
