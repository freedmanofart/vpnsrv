import os
import base64
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from unittest.mock import AsyncMock, patch
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
    Payment,
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

    async def test_root_serves_landing_and_admin_stays_protected(self) -> None:
        response = await self.client.get("/", follow_redirects=False)
        self.assertEqual(200, response.status_code)
        self.assertIn("Freedom VPN", response.text)
        self.assertIn("Control plane", response.text)
        unauthenticated = await self.client.get("/admin")
        self.assertEqual(401, unauthenticated.status_code)
        authenticated = await self.client.get("/admin", auth=self.admin_auth)
        self.assertEqual(200, authenticated.status_code)

    async def test_web_registration_emails_hashed_magic_link_and_opens_cabinet(self) -> None:
        with patch("app.api.routes.web.send_cabinet_link", new=AsyncMock()) as send:
            registered = await self.client.post(
                "/web/register",
                json={"email": "Web.User@example.com", "plan_id": 1},
            )
        self.assertEqual(200, registered.status_code, registered.text)
        link = send.await_args.args[1]
        self.assertIn("/cabinet/access/", link)
        raw_token = link.rsplit("/", 1)[-1]

        from app.db.models.cabinet_access import CabinetAccessToken

        async with self.session_factory() as db:
            web_user = await db.scalar(select(User).where(User.email == "web.user@example.com"))
            self.assertIsNotNone(web_user)
            access = await db.scalar(select(CabinetAccessToken).where(CabinetAccessToken.user_id == web_user.id))
            self.assertNotEqual(raw_token, access.token_hash)

        opened = await self.client.get(f"/cabinet/access/{raw_token}", follow_redirects=False)
        self.assertEqual(303, opened.status_code)
        self.assertEqual("/cabinet", opened.headers["location"])
        cabinet = await self.client.get("/cabinet")
        self.assertEqual(200, cabinet.status_code, cabinet.text)
        self.assertIn("web.user@example.com", cabinet.text)

    async def test_web_cabinet_creates_payment_and_uploads_receipt(self) -> None:
        from app.db.models import CabinetAccessToken, PaymentMethod
        from app.core.tokens import token_hash

        raw = "web-test-token"
        async with self.session_factory() as db:
            user = await db.get(User, self.user_id)
            user.email = "paid@example.com"
            db.add(CabinetAccessToken(user_id=user.id, token_hash=token_hash(raw), expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
            db.add(PaymentMethod(code="sber_qr", name="Сбербанк QR", is_active=True, sort_order=1, image_data=b"qr", image_mime_type="image/png"))
            await db.commit()
        self.client.cookies.set("freedom_cabinet", raw)
        created = await self.client.post(
            "/web/payments/manual",
            json={"plan_id": 1, "node_id": self.node_id, "method_code": "sber_qr"},
        )
        self.assertEqual(200, created.status_code, created.text)
        self.assertIn("qr_url", created.json())
        receipt = await self.client.post(
            f"/web/payments/{created.json()['payment_id']}/receipt",
            json={"filename": "receipt.png", "mime_type": "image/png", "data_base64": base64.b64encode(b"receipt").decode()},
        )
        self.assertEqual(200, receipt.status_code, receipt.text)
        self.assertEqual("processing", receipt.json()["status"])

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
                "max_connections": 3,
                "traffic_limit_gb": 250,
                "price": "2.00",
                "currency": "USD",
            },
        )
        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual(3, created.json()["max_connections"])
        self.assertEqual(250, created.json()["traffic_limit_gb"])
        plan_id = created.json()["id"]
        deleted = await self.client.delete(
            f"/plans/{plan_id}", headers=self.service_headers
        )
        self.assertEqual(204, deleted.status_code, deleted.text)
        async with self.session_factory() as db:
            self.assertIsNone(await db.get(Plan, plan_id))

    async def test_admin_can_cancel_pending_payment(self) -> None:
        async with self.session_factory() as db:
            payment = Payment(
                user_id=self.user_id,
                plan_id=1,
                node_id=self.node_id,
                provider="mock",
                provider_payment_id="admin-cancel-test",
                idempotency_key="admin-cancel-test",
                amount=Decimal("1.00"),
                currency="USD",
                status="pending",
                client_type="universal",
                flow="",
                fingerprint="firefox",
                details={},
            )
            db.add(payment)
            await db.commit()
            await db.refresh(payment)
            payment_id = payment.id

        response = await self.client.post(
            f"/admin/payments/{payment_id}/status",
            auth=self.admin_auth,
            json={"status": "cancelled"},
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("cancelled", response.json()["status"])

        invalid = await self.client.post(
            f"/admin/payments/{payment_id}/status",
            auth=self.admin_auth,
            json={"status": "deleted"},
        )
        self.assertEqual(422, invalid.status_code, invalid.text)

    async def test_admin_manages_public_payment_methods(self) -> None:
        created = await self.client.post(
            "/payment-methods",
            auth=self.admin_auth,
            json={"code": "sbp", "name": "СБП, QR (рубли)", "sort_order": 10},
        )
        self.assertEqual(200, created.status_code, created.text)
        method_id = created.json()["id"]
        image = await self.client.put(
            f"/payment-methods/{method_id}/image",
            auth=self.admin_auth,
            json={
                "filename": "qr.png",
                "mime_type": "image/png",
                "data_base64": base64.b64encode(b"fake-png").decode(),
            },
        )
        self.assertEqual(200, image.status_code, image.text)
        self.assertTrue(image.json()["has_image"])
        downloaded = await self.client.get(
            f"/payment-methods/{method_id}/image", headers=self.service_headers
        )
        self.assertEqual(b"fake-png", downloaded.content)
        self.assertEqual("image/png", downloaded.headers["content-type"])
        visible = await self.client.get("/payment-methods", headers=self.service_headers)
        self.assertEqual(["sbp"], [item["code"] for item in visible.json()])
        disabled = await self.client.patch(
            f"/payment-methods/{method_id}",
            auth=self.admin_auth,
            json={"is_active": False},
        )
        self.assertEqual(200, disabled.status_code, disabled.text)
        visible = await self.client.get("/payment-methods", headers=self.service_headers)
        self.assertEqual([], visible.json())

    async def test_manual_bank_receipt_waits_for_admin_confirmation(self) -> None:
        created = await self.client.post(
            "/payments/manual",
            headers=self.service_headers,
            json={
                "user_id": self.user_id,
                "plan_id": 1,
                "node_id": self.node_id,
                "method_code": "sber_qr",
                "idempotency_key": "manual-receipt-test",
            },
        )
        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual("pending", created.json()["status"])
        payment_id = created.json()["id"]
        receipt = await self.client.post(
            f"/payments/{payment_id}/receipt",
            headers=self.service_headers,
            json={
                "user_id": self.user_id,
                "telegram_file_id": "telegram-file-id",
                "telegram_file_unique_id": "unique-id",
                "media_type": "photo",
                "filename": "receipt.jpg",
                "mime_type": "image/jpeg",
                "data_base64": base64.b64encode(b"fake-receipt").decode(),
            },
        )
        self.assertEqual(200, receipt.status_code, receipt.text)
        self.assertEqual("processing", receipt.json()["status"])
        self.assertEqual(
            "telegram-file-id",
            receipt.json()["details"]["receipt"]["telegram_file_id"],
        )
        downloaded = await self.client.get(
            f"/admin/payments/{payment_id}/receipt", auth=self.admin_auth
        )
        self.assertEqual(200, downloaded.status_code, downloaded.text)
        self.assertEqual(b"fake-receipt", downloaded.content)

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
