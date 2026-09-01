import os
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.services.country import country_from_ip


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"success": True, "country": "Швеция", "country_code": "SE"}


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.url = url
        self.request_kwargs = kwargs
        return FakeResponse()


class CountryTests(IsolatedAsyncioTestCase):
    async def test_country_is_detected_for_public_ip(self):
        with patch("app.services.country.httpx.AsyncClient", FakeClient):
            self.assertEqual("SE|Швеция", await country_from_ip("89.127.212.239"))

    async def test_private_ip_is_rejected_without_external_request(self):
        with self.assertRaisesRegex(ValueError, "публичный IP"):
            await country_from_ip("192.168.10.60")
