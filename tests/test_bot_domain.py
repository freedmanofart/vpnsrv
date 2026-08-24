import unittest

from bot.app.domain import country_label, profile_flow, subscription_payload


class CountryTests(unittest.TestCase):
    def test_supported_countries_and_aliases(self):
        self.assertEqual(country_label("US"), "🇺🇸 США")
        self.assertEqual(country_label("Нидерланды"), "🇳🇱 Нидерланды")
        self.assertEqual(country_label(" germany "), "🇩🇪 Германия")

    def test_unknown_country_is_hidden(self):
        self.assertIsNone(country_label("France"))


class ProfileTests(unittest.TestCase):
    def test_profiles(self):
        self.assertEqual(profile_flow("standard"), "")
        self.assertEqual(profile_flow("vision"), "xtls-rprx-vision")

    def test_purchase_payload_for_amnezia(self):
        self.assertEqual(
            subscription_payload(1, 2, 3, "amnezia", "vision"),
            {
                "user_id": 1,
                "plan_id": 2,
                "node_id": 3,
                "client_type": "amnezia",
                "flow": "xtls-rprx-vision",
                "fingerprint": "chrome",
            },
        )

    def test_invalid_options(self):
        with self.assertRaises(ValueError):
            profile_flow("tls")
        with self.assertRaises(ValueError):
            subscription_payload(1, 2, 3, "unknown", "standard")


if __name__ == "__main__":
    unittest.main()
