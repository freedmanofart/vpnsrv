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

    async def test_root_redirects_to_protected_admin(self) -> None:
        response = await self.client.get("/", follow_redirects=False)
        self.assertEqual(307, response.status_code)
        self.assertEqual("/admin", response.headers["location"])
        unauthenticated = await self.client.get("/admin")
        self.assertEqual(401, unauthenticated.status_code)
        authenticated = await self.client.get("/admin", auth=self.admin_auth)
        self.assertEqual(200, authenticated.status_code)

    async def test_plan_delete_rejects_used_and_removes_unused(self) -> None:
        used = await self.client.delete("/plans/1", headers=self.service_headers)
        self.assertEqual(409, used.status_code, used.text)

        created = await self.client.post(
            "/plans",
            headers=self.service_headers,
            json={
                "code": "unused-plan",
                "name": "Unused",
                "duration_days": 5,
                "price": "2.00",
                "currency": "USD",
            },
        )
        self.assertEqual(200, created.status_code, created.text)
        plan_id = created.json()["id"]
        deleted = await self.client.delete(
            f"/plans/{plan_id}", headers=self.service_headers
        )
        self.assertEqual(204, deleted.status_code, deleted.text)
        async with self.session_factory() as db:
            self.assertIsNone(await db.get(Plan, plan_id))

    async def test_promo_extends_subscription_once(self) -> None:
        async with self.session_factory() as db:
            subscription = (
                await db.execute(
                    select(Subscription).where(Subscription.user_id == self.user_id)
                )
            ).scalar_one()
            before = subscription.expires_at
        response = await self.client.post(
            "/subscriptions/access-grants",
            headers=self.service_headers,
            json={
                "telegram_id": self.telegram_id,
                "kind": "promo",
                "code": "WELCOME7",
                "node_id": self.node_id,
                "client_type": "amnezia",
                "flow": "xtls-rprx-vision",
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        extended = datetime.fromisoformat(response.json()["expires_at"])
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)
        if extended.tzinfo is None:
            extended = extended.replace(tzinfo=timezone.utc)
        self.assertEqual(timedelta(days=7), extended - before)
        repeated = await self.client.post(
            "/subscriptions/access-grants",
            headers=self.service_headers,
            json={
                "telegram_id": self.telegram_id,
                "kind": "promo",
                "code": "WELCOME7",
                "node_id": self.node_id,
            },
        )
        self.assertEqual(409, repeated.status_code)

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
        snapshot = await self.client.post(
            f"/admin/debug-sessions/{debug.json()['id']}/snapshot",
            auth=self.admin_auth,
            json={"secrets": {"bot_token": "fake-debug-token"}},
        )
        self.assertEqual(200, snapshot.status_code, snapshot.text)
        self.assertTrue(snapshot.json()["sensitive"])
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
