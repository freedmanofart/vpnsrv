import json
import os
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")

from app.services.threexui import ThreeXUIClient, ThreeXUIError


class FakeAsyncClient:
    requests = []
    responses = []

    def __init__(self, **kwargs):
        self.options = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs, self.options))
        payload = self.responses.pop(0)
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))


class ThreeXUIClientTests(IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAsyncClient.requests = []
        FakeAsyncClient.responses = []

    async def test_add_and_remove_client_use_master_api(self):
        FakeAsyncClient.responses = [
            {"success": True, "msg": "Client added"},
            {"success": True, "msg": "Client deleted"},
        ]
        client = ThreeXUIClient("https://master.example/hidden", timeout=4)
        with (
            patch("app.services.threexui.settings.threexui_api_token", "node-token"),
            patch("app.services.threexui.httpx.AsyncClient", FakeAsyncClient),
        ):
            await client.add_vless_user(
                "17", "uuid-1", "vpn-42", flow="xtls-rprx-vision",
                expiry_time=1893456000000, telegram_id=12345, limit_ip=3,
                total_gb=250 * 1024 * 1024 * 1024,
            )
            await client.remove_vless_user("17", "vpn-42")

        add = FakeAsyncClient.requests[0]
        self.assertEqual("https://master.example/hidden/panel/api/clients/add", add[1])
        self.assertEqual([17], add[2]["json"]["inboundIds"])
        self.assertEqual("uuid-1", add[2]["json"]["client"]["id"])
        self.assertEqual(1893456000000, add[2]["json"]["client"]["expiryTime"])
        self.assertEqual(12345, add[2]["json"]["client"]["tgId"])
        self.assertEqual(3, add[2]["json"]["client"]["limitIp"])
        self.assertEqual(250 * 1024 * 1024 * 1024, add[2]["json"]["client"]["totalGB"])
        self.assertEqual("Bearer node-token", add[2]["headers"]["Authorization"])
        self.assertIn("clients/del/vpn-42?keepTraffic=1", FakeAsyncClient.requests[1][1])

    async def test_update_client_replaces_full_payload(self):
        FakeAsyncClient.responses = [{"success": True, "msg": "Client updated"}]
        with (
            patch("app.services.threexui.settings.threexui_api_token", "node-token"),
            patch("app.services.threexui.httpx.AsyncClient", FakeAsyncClient),
        ):
            await ThreeXUIClient("https://master.example/base").update_vless_user(
                "17", "uuid-1", "vpn-42", flow="xtls-rprx-vision",
                expiry_time=1893456000000, limit_ip=1,
                total_gb=3 * 1024 * 1024 * 1024,
            )

        request = FakeAsyncClient.requests[0]
        self.assertTrue(request[1].endswith("/panel/api/clients/update/vpn-42"))
        self.assertEqual("uuid-1", request[2]["json"]["id"])
        self.assertEqual(1893456000000, request[2]["json"]["expiryTime"])
        self.assertEqual(3 * 1024 * 1024 * 1024, request[2]["json"]["totalGB"])

    async def test_get_users_selects_configured_inbound(self):
        FakeAsyncClient.responses = [{
            "success": True,
            "obj": [
                {"id": 5, "settings": {"clients": [{"email": "other"}]}},
                {"id": 17, "settings": {"clients": [
                    {"email": "vpn-42", "id": "uuid-1", "flow": ""}
                ]}},
            ],
        }]
        with (
            patch("app.services.threexui.settings.threexui_api_token", "node-token"),
            patch("app.services.threexui.httpx.AsyncClient", FakeAsyncClient),
        ):
            users = await ThreeXUIClient("https://master.example/base").get_users("17")
        self.assertEqual(["vpn-42"], [user.email for user in users])
        self.assertTrue(FakeAsyncClient.requests[0][1].endswith("/panel/api/inbounds/list"))

    async def test_get_client_subscription_info_collects_attached_inbounds(self):
        FakeAsyncClient.responses = [{
            "success": True,
            "obj": [
                {"id": 8, "settings": {"clients": [
                    {"email": "vpn-42", "subId": "sub-42"},
                ]}},
                {"id": 12, "settings": json.dumps({"clients": [
                    {"email": "vpn-42", "subId": "sub-42"},
                ]})},
                {"id": 13, "settings": {"clients": [
                    {"email": "other", "subId": "other-sub"},
                ]}},
            ],
        }]
        with (
            patch("app.services.threexui.settings.threexui_api_token", "node-token"),
            patch("app.services.threexui.httpx.AsyncClient", FakeAsyncClient),
        ):
            info = await ThreeXUIClient("https://master.example/base").get_client_subscription_info(
                "vpn-42"
            )

        self.assertEqual({"sub_id": "sub-42", "inbound_ids": [8, 12]}, info)

    async def test_get_client_subscription_info_rejects_conflicting_subids(self):
        FakeAsyncClient.responses = [{
            "success": True,
            "obj": [
                {"id": 8, "settings": {"clients": [{"email": "vpn-42", "subId": "first"}]}},
                {"id": 12, "settings": {"clients": [{"email": "vpn-42", "subId": "second"}]}},
            ],
        }]
        with (
            patch("app.services.threexui.settings.threexui_api_token", "node-token"),
            patch("app.services.threexui.httpx.AsyncClient", FakeAsyncClient),
        ):
            with self.assertRaises(ThreeXUIError):
                await ThreeXUIClient("https://master.example/base").get_client_subscription_info("vpn-42")

    async def test_get_client_traffic_uses_email_endpoint(self):
        FakeAsyncClient.responses = [{
            "success": True,
            "obj": [{"id": 8, "clientStats": [
                {"email": "vpn-42", "up": 1024, "down": 2048, "total": 4096}
            ]}],
        }]
        with (
            patch("app.services.threexui.settings.threexui_api_token", "node-token"),
            patch("app.services.threexui.httpx.AsyncClient", FakeAsyncClient),
        ):
            traffic = await ThreeXUIClient("https://master.example/base").get_client_traffic("vpn-42")
        self.assertEqual(3072, traffic["up"] + traffic["down"])
        self.assertTrue(FakeAsyncClient.requests[0][1].endswith("/panel/api/inbounds/list"))
