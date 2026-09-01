import os
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.services.threexui import ThreeXUIClient


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
                expiry_time=1893456000000, telegram_id=12345,
            )
            await client.remove_vless_user("17", "vpn-42")

        add = FakeAsyncClient.requests[0]
        self.assertEqual("https://master.example/hidden/panel/api/clients/add", add[1])
        self.assertEqual([17], add[2]["json"]["inboundIds"])
        self.assertEqual("uuid-1", add[2]["json"]["client"]["id"])
        self.assertEqual(1893456000000, add[2]["json"]["client"]["expiryTime"])
        self.assertEqual(12345, add[2]["json"]["client"]["tgId"])
        self.assertEqual("Bearer node-token", add[2]["headers"]["Authorization"])
        self.assertIn("clients/del/vpn-42?keepTraffic=1", FakeAsyncClient.requests[1][1])

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
