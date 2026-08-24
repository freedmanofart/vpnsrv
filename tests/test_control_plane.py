import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from unittest import IsolatedAsyncioTestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SERVICE_API_TOKEN", "test-service-token")

import app.main as main_module
from app.db.base import Base
from app.db.models import (
    AuditLog,
    Plan,
    Subscription,
    User,
    VPNClient,
    VPNNode,
    VPNNodeConfig,
)
from app.db.session import get_db


class ControlPlaneTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.session_factory() as db:
            user = User(telegram_id=42424242, username="device-user", status="active")
            plan = Plan(
                code="control-plane",
                name="Control plane",
                duration_days=30,
                price=Decimal("1.00"),
                currency="USD",
                is_active=True,
            )
            node = VPNNode(
                name="agent-node",
                provider="test",
                region="de",
                hostname="de.example.test",
                ip_address="203.0.113.20",
                status="active",
                capacity=100,
            )
            db.add_all([user, plan, node])
            await db.flush()
            subscription = Subscription(
                user_id=user.id,
                plan_id=plan.id,
                status="active",
                starts_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            )
            db.add(subscription)
            await db.flush()
            client = VPNClient(
                user_id=user.id,
                subscription_id=subscription.id,
                node_id=node.id,
                protocol="vless",
                client_type="amnezia",
                flow="xtls-rprx-vision",
                fingerprint="chrome",
                client_uuid="11111111-2222-3333-4444-555555555555",
                status="active",
                expires_at=subscription.expires_at,
            )
            config = VPNNodeConfig(
                node_id=node.id,
                protocol="vless",
                config={
                    "api_address": "127.0.0.1:10085",
                    "inbound_tag": "vless-reality",
                    "host": "de.example.test",
                    "port": 443,
                    "type": "tcp",
                    "security": "reality",
                    "sni": "example.com",
                    "fp": "chrome",
                    "pbk": "public-key",
                    "sid": "abcd",
                },
            )
            db.add_all([client, config])
            await db.commit()
            self.user_id = user.id
            self.telegram_id = user.telegram_id
            self.node_id = node.id

        async def override_db():
            async with self.session_factory() as db:
                yield db

        main_module.app.dependency_overrides[get_db] = override_db
        self.original_audit_factory = main_module.AsyncSessionLocal
        main_module.AsyncSessionLocal = self.session_factory
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app),
            base_url="http://test",
        )
        self.admin_auth = ("admin", "change_me")
        self.service_headers = {"Authorization": "Bearer test-service-token"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        main_module.app.dependency_overrides.clear()
        main_module.AsyncSessionLocal = self.original_audit_factory
        await self.engine.dispose()

    async def test_node_agent_credential_state_and_status(self) -> None:
        credential = await self.client.post(
            f"/agent/v1/credentials/{self.node_id}/rotate",
            auth=self.admin_auth,
        )
        self.assertEqual(200, credential.status_code, credential.text)
        token = credential.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        state = await self.client.get("/agent/v1/state", headers=headers)
        self.assertEqual(200, state.status_code, state.text)
        self.assertEqual("vpn-1", state.json()["clients"][0]["email"])
        report = await self.client.post(
            "/agent/v1/status",
            headers=headers,
            json={
                "status": "online",
                "latency_ms": 12.5,
                "xray_users": 1,
                "active_connections": 1,
                "restored": 0,
                "removed": 0,
                "errors": [],
            },
        )
        self.assertEqual(200, report.status_code, report.text)
        async with self.session_factory() as db:
            node = await db.get(VPNNode, self.node_id)
            self.assertEqual("online", node.health_status)
            self.assertEqual(12.5, node.latency_ms)

    async def test_device_activation_profile_refresh_and_sensitive_debug(self) -> None:
        code_response = await self.client.post(
            "/v1/client/activation-codes",
            headers=self.service_headers,
            json={"telegram_id": self.telegram_id, "ttl_minutes": 10},
        )
        self.assertEqual(200, code_response.status_code, code_response.text)
        activation = await self.client.post(
            "/v1/client/activate",
            json={
                "code": code_response.json()["code"],
                "name": "Test phone",
                "platform": "android",
            },
        )
        self.assertEqual(200, activation.status_code, activation.text)
        old_token = activation.json()["access_token"]
        device_headers = {"Authorization": f"Bearer {old_token}"}
        profile = await self.client.get("/v1/client/profile", headers=device_headers)
        self.assertEqual(200, profile.status_code, profile.text)
        self.assertIn("flow=xtls-rprx-vision", profile.json()["nodes"][0]["config"])

        debug = await self.client.post(
            "/admin/debug-sessions",
            auth=self.admin_auth,
            json={"reason": "control plane test", "duration_minutes": 5},
        )
        self.assertEqual(200, debug.status_code, debug.text)
        profile = await self.client.get("/v1/client/profile", headers=device_headers)
        self.assertEqual(200, profile.status_code, profile.text)
        async with self.session_factory() as db:
            result = await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "device.profile.read")
                .order_by(AuditLog.id.desc())
            )
            latest = result.scalars().first()
            self.assertTrue(latest.sensitive)
            self.assertIn("vless://", latest.details["configs"][0]["vpn_uri"])

        refreshed = await self.client.post("/v1/client/refresh", headers=device_headers)
        self.assertEqual(200, refreshed.status_code, refreshed.text)
        new_token = refreshed.json()["access_token"]
        rejected = await self.client.get("/v1/client/profile", headers=device_headers)
        self.assertEqual(401, rejected.status_code)
        accepted = await self.client.get(
            "/v1/client/profile",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        self.assertEqual(200, accepted.status_code, accepted.text)
