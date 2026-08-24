import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SERVICE_API_TOKEN", "test-service-token")

from app.api.routes.subscriptions import renew_subscription
from app.db.base import Base
from app.db.models import (
    Payment,
    PaymentEvent,
    Plan,
    Subscription,
    User,
    VPNClient,
    VPNNode,
    VPNNodeConfig,
)
from app.schemas.payment import PaymentCreate
from app.services.payments import (
    PaymentInvalidTransition,
    PaymentProvisioningError,
    create_payment,
    process_payment_event,
)
from app.services.provisioning import commit_provisioning, provision_subscription
from app.services.reconciliation import reconcile_node
from app.services.vpn_expiration import expire_desired_state, expire_subscriptions
from app.services.xray import (
    XrayError,
    XrayUserAlreadyExists,
    XrayUserNotFound,
)


class FakeXray:
    users: dict[str, dict[str, SimpleNamespace]] = {}
    fail_add = False
    fail_remove: set[str] = set()

    def __init__(self, address: str | None = None, timeout: float = 3.0):
        self.address = address or "fallback:10085"
        self.timeout = timeout
        self.users.setdefault(self.address, {})

    @classmethod
    def reset(cls) -> None:
        cls.users = {}
        cls.fail_add = False
        cls.fail_remove = set()

    async def add_vless_user(
        self,
        inbound_tag: str,
        client_uuid: str,
        email: str,
        level: int = 0,
        flow: str = "",
    ) -> None:
        if self.fail_add:
            raise XrayError("simulated add failure")
        users = self.users[self.address]
        if email in users:
            raise XrayUserAlreadyExists(email)
        users[email] = SimpleNamespace(
            email=email,
            inbound_tag=inbound_tag,
            client_uuid=client_uuid,
            level=level,
            flow=flow,
        )

    async def remove_vless_user(self, inbound_tag: str, email: str) -> None:
        if email in self.fail_remove:
            self.fail_remove.remove(email)
            raise XrayError("simulated remove failure")
        users = self.users[self.address]
        if email not in users:
            raise XrayUserNotFound(email)
        del users[email]

    async def get_users(self, inbound_tag: str) -> list[SimpleNamespace]:
        return list(self.users[self.address].values())


class VPNLifecycleTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeXray.reset()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with self.session_factory() as db:
            user = User(
                telegram_id=123456789,
                username="lifecycle-test",
                status="active",
            )
            plan = Plan(
                code="month",
                name="Month",
                duration_days=30,
                price=Decimal("10.00"),
                currency="USD",
                is_active=True,
            )
            node = VPNNode(
                name="test-node",
                provider="test",
                region="DE",
                hostname="vpn.example.test",
                ip_address="203.0.113.10",
                status="active",
                capacity=100,
            )
            db.add_all([user, plan, node])
            await db.flush()
            db.add(
                VPNNodeConfig(
                    node_id=node.id,
                    protocol="vless",
                    config={
                        "api_address": "node-grpc:10085",
                        "inbound_tag": "vless-reality",
                        "host": "vpn.example.test",
                        "port": 443,
                        "type": "tcp",
                        "security": "reality",
                        "sni": "example.com",
                        "fp": "chrome",
                        "pbk": "public-key",
                        "sid": "abcd",
                    },
                )
            )
            await db.commit()
            self.user_id = user.id
            self.plan_id = plan.id
            self.node_id = node.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def payment_data(self, key: str) -> PaymentCreate:
        return PaymentCreate(
            user_id=self.user_id,
            plan_id=self.plan_id,
            node_id=self.node_id,
            client_type="amnezia",
            flow="xtls-rprx-vision",
            fingerprint="chrome",
            idempotency_key=key,
        )

    async def provision(self, db):
        result = await provision_subscription(
            db,
            user_id=self.user_id,
            plan_id=self.plan_id,
            node_id=self.node_id,
            client_type="amnezia",
            flow="xtls-rprx-vision",
            fingerprint="chrome",
            xray_factory=FakeXray,
        )
        await commit_provisioning(db, result)
        return result

    async def test_paid_event_creates_amnezia_client_once(self) -> None:
        async with self.session_factory() as db:
            payment = await create_payment(
                db,
                self.payment_data("telegram:callback-1"),
                provider="mock",
            )
            repeated = await create_payment(
                db,
                self.payment_data("telegram:callback-1"),
                provider="mock",
            )
            self.assertEqual(payment.id, repeated.id)

            paid = await process_payment_event(
                db,
                provider="mock",
                event_id="provider-event-1",
                provider_payment_id=payment.provider_payment_id,
                target_status="paid",
                payload={"details": {"source": "test"}},
                xray_factory=FakeXray,
            )
            duplicate = await process_payment_event(
                db,
                provider="mock",
                event_id="provider-event-1",
                provider_payment_id=payment.provider_payment_id,
                target_status="paid",
                payload={"details": {"source": "test"}},
                xray_factory=FakeXray,
            )

            self.assertEqual("paid", duplicate.status)
            self.assertEqual(paid.subscription_id, duplicate.subscription_id)
            client = (
                await db.execute(
                    select(VPNClient).where(
                        VPNClient.subscription_id == paid.subscription_id
                    )
                )
            ).scalar_one()
            self.assertEqual("amnezia", client.client_type)
            self.assertEqual("xtls-rprx-vision", client.flow)
            self.assertIn(f"vpn-{client.id}", FakeXray.users["node-grpc:10085"])
            self.assertEqual(
                1,
                await db.scalar(select(func.count()).select_from(PaymentEvent)),
            )

    async def test_payment_state_machine_rejects_terminal_transition(self) -> None:
        async with self.session_factory() as db:
            payment = await create_payment(
                db,
                self.payment_data("telegram:callback-2"),
                provider="mock",
            )
            payment_id = payment.id
            await process_payment_event(
                db,
                provider="mock",
                event_id="processing-1",
                provider_payment_id=payment.provider_payment_id,
                target_status="processing",
                payload={},
            )
            await process_payment_event(
                db,
                provider="mock",
                event_id="failed-1",
                provider_payment_id=payment.provider_payment_id,
                target_status="failed",
                payload={},
            )
            with self.assertRaises(PaymentInvalidTransition):
                await process_payment_event(
                    db,
                    provider="mock",
                    event_id="late-paid-1",
                    provider_payment_id=payment.provider_payment_id,
                    target_status="paid",
                    payload={},
                    xray_factory=FakeXray,
                )

            stored = await db.get(Payment, payment_id)
            self.assertEqual("failed", stored.status)
            self.assertEqual(
                2,
                await db.scalar(select(func.count()).select_from(PaymentEvent)),
            )

    async def test_xray_add_failure_rolls_back_paid_provisioning(self) -> None:
        async with self.session_factory() as db:
            payment = await create_payment(
                db,
                self.payment_data("telegram:callback-3"),
                provider="mock",
            )
            payment_id = payment.id
            FakeXray.fail_add = True
            with self.assertRaises(PaymentProvisioningError):
                await process_payment_event(
                    db,
                    provider="mock",
                    event_id="paid-with-xray-down",
                    provider_payment_id=payment.provider_payment_id,
                    target_status="paid",
                    payload={},
                    xray_factory=FakeXray,
                )

            stored = await db.get(Payment, payment_id)
            self.assertEqual("pending", stored.status)
            self.assertEqual(
                0,
                await db.scalar(select(func.count()).select_from(Subscription)),
            )
            self.assertEqual(
                0,
                await db.scalar(select(func.count()).select_from(VPNClient)),
            )
            self.assertEqual(
                0,
                await db.scalar(select(func.count()).select_from(PaymentEvent)),
            )

    async def test_renew_replaces_client_and_extends_from_current_expiry(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            old_client_id = initial.client.id
            old_expiry = initial.subscription.expires_at

            with patch("app.api.routes.subscriptions.XrayClient", FakeXray):
                renewed = await renew_subscription(initial.subscription.id, db)

            clients = (
                await db.execute(
                    select(VPNClient)
                    .where(VPNClient.subscription_id == renewed.id)
                    .order_by(VPNClient.id)
                )
            ).scalars().all()
            self.assertEqual(2, len(clients))
            self.assertEqual("revoked", clients[0].status)
            self.assertEqual("active", clients[1].status)
            self.assertEqual(
                (old_expiry + timedelta(days=30)).replace(tzinfo=None),
                renewed.expires_at.replace(tzinfo=None),
            )
            users = FakeXray.users["node-grpc:10085"]
            self.assertNotIn(f"vpn-{old_client_id}", users)
            self.assertIn(f"vpn-{clients[1].id}", users)

    async def test_renew_compensates_when_old_xray_user_cannot_be_removed(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            subscription_id = initial.subscription.id
            old_email = f"vpn-{initial.client.id}"
            FakeXray.fail_remove.add(old_email)

            with patch("app.api.routes.subscriptions.XrayClient", FakeXray):
                with self.assertRaises(HTTPException) as raised:
                    await renew_subscription(subscription_id, db)
            self.assertEqual(502, raised.exception.status_code)

            clients = (
                await db.execute(
                    select(VPNClient).where(
                        VPNClient.subscription_id == subscription_id
                    )
                )
            ).scalars().all()
            self.assertEqual(1, len(clients))
            self.assertEqual("active", clients[0].status)
            self.assertEqual({old_email}, set(FakeXray.users["node-grpc:10085"]))

    async def test_expiration_revokes_xray_and_database_client(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            initial.subscription.expires_at = expired_at
            initial.client.expires_at = expired_at
            await db.commit()

            count = await expire_subscriptions(db, xray_factory=FakeXray)
            self.assertEqual(1, count)
            await db.refresh(initial.subscription)
            await db.refresh(initial.client)
            self.assertEqual("expired", initial.subscription.status)
            self.assertEqual("revoked", initial.client.status)
            self.assertNotIn(
                f"vpn-{initial.client.id}",
                FakeXray.users["node-grpc:10085"],
            )

    async def test_agent_expiration_updates_desired_state_without_direct_xray(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            email = f"vpn-{initial.client.id}"
            expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            initial.subscription.expires_at = expired_at
            initial.client.expires_at = expired_at
            await db.commit()

            subscriptions, clients = await expire_desired_state(db)

            self.assertEqual((1, 1), (subscriptions, clients))
            await db.refresh(initial.subscription)
            await db.refresh(initial.client)
            self.assertEqual("expired", initial.subscription.status)
            self.assertEqual("revoked", initial.client.status)
            self.assertIn(email, FakeXray.users["node-grpc:10085"])

    async def test_reconciliation_restores_after_xray_restart_and_removes_orphan(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            users = FakeXray.users["node-grpc:10085"]
            users.clear()
            users["vpn-9999"] = SimpleNamespace(email="vpn-9999")
            users["vpn-test"] = SimpleNamespace(email="vpn-test")

            report = await reconcile_node(
                db,
                self.node_id,
                xray_factory=FakeXray,
            )

            self.assertEqual(1, report.restored)
            self.assertEqual(1, report.removed)
            self.assertIn(f"vpn-{initial.client.id}", users)
            self.assertIn("vpn-test", users)
            self.assertNotIn("vpn-9999", users)
