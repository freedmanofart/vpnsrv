import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("SERVICE_API_TOKEN", "test-service-token")

from app.api.routes.client import alternate_client_profiles
from app.api.routes.subscriptions import renew_subscription, rotate_subscription_client
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
from app.schemas.subscription import VPNClientRotate
from app.services.payments import (
    PaymentInvalidTransition,
    PaymentProvisioningError,
    create_payment,
    process_payment_event,
)
from app.services.provisioning import (
    commit_provisioning,
    provision_subscription,
    renew_paid_subscription,
)
from app.services.reconciliation import reconcile_node
from app.services.vpn_expiration import expire_desired_state, expire_subscriptions
from app.services.threexui import (
    ThreeXUIError,
    ThreeXUIClientAlreadyExists,
    ThreeXUIClientNotFound,
)


class FakePanel:
    users: dict[str, dict[str, SimpleNamespace]] = {}
    fail_add = False
    fail_remove: set[str] = set()
    fail_update: set[str] = set()

    def __init__(self, address: str | None = None, timeout: float = 3.0):
        self.address = address or "fallback:10085"
        self.timeout = timeout
        self.users.setdefault(self.address, {})

    @classmethod
    def reset(cls) -> None:
        cls.users = {}
        cls.fail_add = False
        cls.fail_remove = set()
        cls.fail_update = set()

    async def add_vless_user(
        self,
        inbound_tag: str,
        client_uuid: str,
        email: str,
        level: int = 0,
        flow: str = "",
        expiry_time: int = 0,
        telegram_id: int = 0,
        limit_ip: int = 0,
        limit_hwid: int = 0,
        total_gb: int = 0,
    ) -> None:
        if self.fail_add:
            raise ThreeXUIError("simulated add failure")
        users = self.users[self.address]
        if email in users:
            raise ThreeXUIClientAlreadyExists(email)
        users[email] = SimpleNamespace(
            email=email,
            inbound_tag=inbound_tag,
            inbound_ids={str(inbound_tag)},
            client_uuid=client_uuid,
            level=level,
            flow=flow,
            expiry_time=expiry_time,
            telegram_id=telegram_id,
            limit_ip=limit_ip,
            limit_hwid=limit_hwid,
            total_gb=total_gb,
        )

    async def remove_vless_user(self, inbound_tag: str, email: str) -> None:
        if email in self.fail_remove:
            self.fail_remove.remove(email)
            raise ThreeXUIError("simulated remove failure")
        users = self.users[self.address]
        if email not in users:
            raise ThreeXUIClientNotFound(email)
        del users[email]

    async def attach_client(self, email: str, inbound_ids: list[int]) -> None:
        user = self.users[self.address].get(email)
        if user is None:
            raise ThreeXUIClientNotFound(email)
        user.inbound_ids.update(str(inbound_id) for inbound_id in inbound_ids)

    async def detach_client(self, email: str, inbound_ids: list[int]) -> None:
        user = self.users[self.address].get(email)
        if user is None:
            raise ThreeXUIClientNotFound(email)
        user.inbound_ids.difference_update(str(inbound_id) for inbound_id in inbound_ids)

    async def update_vless_user(
        self,
        inbound_tag: str,
        client_uuid: str,
        email: str,
        level: int = 0,
        flow: str = "",
        expiry_time: int = 0,
        telegram_id: int = 0,
        limit_ip: int = 0,
        limit_hwid: int = 0,
        total_gb: int = 0,
    ) -> None:
        del inbound_tag, level
        if email in self.fail_update:
            self.fail_update.remove(email)
            raise ThreeXUIError("simulated update failure")
        users = self.users[self.address]
        if email not in users:
            raise ThreeXUIClientNotFound(email)
        users[email].client_uuid = client_uuid
        users[email].flow = flow
        users[email].expiry_time = expiry_time
        users[email].telegram_id = telegram_id
        users[email].limit_ip = limit_ip
        users[email].limit_hwid = limit_hwid
        users[email].total_gb = total_gb

    async def get_users(self, inbound_tag: str) -> list[SimpleNamespace]:
        return [
            user
            for user in self.users[self.address].values()
            if str(inbound_tag) in getattr(user, "inbound_ids", {str(inbound_tag)})
        ]


class VPNLifecycleTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakePanel.reset()
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
                traffic_limit_gb=250,
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
                        "api_address": "https://master.example/base",
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
            panel_factory=FakePanel,
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
            payment.details = {
                "source": "web_cabinet",
                "platega": {"redirect": "https://platega.test/pay"},
            }
            await db.commit()
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
                payload={"details": {"source": "platega_callback", "platega": {"status": "CONFIRMED"}}},
                panel_factory=FakePanel,
            )
            duplicate = await process_payment_event(
                db,
                provider="mock",
                event_id="provider-event-1",
                provider_payment_id=payment.provider_payment_id,
                target_status="paid",
                payload={"details": {"source": "test"}},
                panel_factory=FakePanel,
            )

            self.assertEqual("paid", duplicate.status)
            self.assertEqual(paid.subscription_id, duplicate.subscription_id)
            self.assertEqual("web_cabinet", duplicate.details["source"])
            self.assertEqual("platega_callback", duplicate.details["last_event_source"])
            self.assertEqual("https://platega.test/pay", duplicate.details["platega"]["redirect"])
            self.assertEqual("CONFIRMED", duplicate.details["platega"]["status"])
            client = (
                await db.execute(
                    select(VPNClient).where(
                        VPNClient.subscription_id == paid.subscription_id
                    )
                )
            ).scalar_one()
            self.assertEqual("amnezia", client.client_type)
            self.assertEqual("xtls-rprx-vision", client.flow)
            self.assertIn(f"vpn-{client.id}", FakePanel.users["https://master.example/base"])
            self.assertEqual(
                1,
                await db.scalar(select(func.count()).select_from(PaymentEvent)),
            )

    async def test_second_paid_order_renews_active_subscription(self) -> None:
        async with self.session_factory() as db:
            first_payment = await create_payment(
                db, self.payment_data("web:first-order"), provider="manual_bank"
            )
            first_paid = await process_payment_event(
                db,
                provider="manual_bank",
                event_id="web-first-paid",
                provider_payment_id=first_payment.provider_payment_id,
                target_status="paid",
                payload={"details": {"source": "web_cabinet"}},
                panel_factory=FakePanel,
            )
            initial = await db.get(Subscription, first_paid.subscription_id)
            initial_client = (
                await db.execute(
                    select(VPNClient).where(
                        VPNClient.subscription_id == initial.id,
                        VPNClient.status == "active",
                    )
                )
            ).scalar_one()
            initial_client_id = initial_client.id
            old_expiry = initial.expires_at
            second_payment = await create_payment(
                db, self.payment_data("web:renew-active"), provider="manual_bank"
            )
            paid = await process_payment_event(
                db,
                provider="manual_bank",
                event_id="web-renew-paid",
                provider_payment_id=second_payment.provider_payment_id,
                target_status="paid",
                payload={"details": {"source": "web_cabinet"}},
                panel_factory=FakePanel,
            )
            self.assertEqual(initial.id, paid.subscription_id)
            renewed = await db.get(Subscription, paid.subscription_id)
            self.assertEqual(
                (old_expiry + timedelta(days=30)).replace(tzinfo=None),
                renewed.expires_at.replace(tzinfo=None),
            )
            clients = (
                await db.execute(
                    select(VPNClient)
                    .where(VPNClient.subscription_id == renewed.id)
                    .order_by(VPNClient.id)
                )
            ).scalars().all()
            self.assertEqual(["active"], [item.status for item in clients])
            self.assertEqual(initial_client_id, clients[0].id)
            self.assertEqual(500, clients[0].traffic_limit_gb)
            self.assertIn(f"vpn-{initial_client_id}", FakePanel.users["https://master.example/base"])
            payments = (
                await db.execute(
                    select(Payment)
                    .where(Payment.subscription_id == renewed.id)
                    .order_by(Payment.id)
                )
            ).scalars().all()
            self.assertEqual(2, len(payments))

    async def test_paid_order_renews_subscription_awaiting_expiration_job(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            expired_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            initial.subscription.expires_at = expired_at
            initial.client.expires_at = expired_at
            await db.commit()

            payment = await create_payment(
                db,
                self.payment_data("web:renew-before-expiration-job"),
                provider="manual_bank",
            )
            paid = await process_payment_event(
                db,
                provider="manual_bank",
                event_id="web-renew-before-expiration-job-paid",
                provider_payment_id=payment.provider_payment_id,
                target_status="paid",
                payload={"details": {"source": "web_cabinet"}},
                panel_factory=FakePanel,
            )

            self.assertEqual(initial.subscription.id, paid.subscription_id)
            renewed = await db.get(Subscription, initial.subscription.id)
            self.assertEqual("active", renewed.status)
            self.assertGreater(
                renewed.expires_at.replace(tzinfo=timezone.utc),
                datetime.now(timezone.utc) + timedelta(days=29),
            )
            clients = (
                await db.execute(
                    select(VPNClient)
                    .where(VPNClient.subscription_id == renewed.id)
                    .order_by(VPNClient.id)
                )
            ).scalars().all()
            self.assertEqual(["active"], [item.status for item in clients])
            self.assertEqual(initial.client.id, clients[0].id)

    async def test_paid_order_revives_expired_subscription_without_new_vpn_id(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            subscription_id = initial.subscription.id
            client_id = initial.client.id
            expired_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            initial.subscription.expires_at = expired_at
            initial.client.expires_at = expired_at
            await db.commit()
            self.assertEqual(1, await expire_subscriptions(db, panel_factory=FakePanel))

            payment = await create_payment(
                db,
                self.payment_data("web:revive-expired"),
                provider="manual_bank",
            )
            paid = await process_payment_event(
                db,
                provider="manual_bank",
                event_id="web-revive-expired-paid",
                provider_payment_id=payment.provider_payment_id,
                target_status="paid",
                payload={"details": {"source": "web_cabinet"}},
                panel_factory=FakePanel,
            )

            self.assertEqual(subscription_id, paid.subscription_id)
            client = await db.get(VPNClient, client_id)
            self.assertEqual("active", client.status)
            self.assertIsNone(client.revoked_at)
            self.assertIn(f"vpn-{client_id}", FakePanel.users["https://master.example/base"])

    async def test_alternate_node_profile_reuses_the_existing_client_identity(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            source_config = await db.scalar(
                select(VPNNodeConfig).where(
                    VPNNodeConfig.node_id == self.node_id,
                    VPNNodeConfig.protocol == "vless",
                )
            )
            alternate = VPNNode(
                name="node-ru",
                provider="test",
                region="RU|Россия",
                hostname="ru.example.test",
                ip_address="203.0.113.11",
                status="active",
                capacity=100,
            )
            db.add(alternate)
            await db.flush()
            db.add(
                VPNNodeConfig(
                    node_id=alternate.id,
                    protocol="vless",
                    config={
                        "api_address": "https://master.example/base",
                        "inbound_tag": "ru-vless",
                        "host": "ru.example.test",
                        "port": 8443,
                        "type": "tcp",
                        "security": "reality",
                        "sni": "example.com",
                        "fp": "chrome",
                        "pbk": "public-key",
                        "sid": "efgh",
                    },
                )
            )
            await db.flush()
            source_config.config = {
                **source_config.config,
                "alternate_node_ids": [alternate.id],
            }

            profiles = await alternate_client_profiles(db, initial.client, source_config)

            self.assertEqual(1, len(profiles))
            self.assertEqual("🇷🇺 Россия (Reality)", profiles[0]["name"])
            parsed = urlsplit(profiles[0]["config"])
            self.assertEqual(("ru.example.test", 8443), (parsed.hostname, parsed.port))
            self.assertEqual(initial.client.client_uuid, parsed.username)

            alternate_config = await db.scalar(
                select(VPNNodeConfig).where(VPNNodeConfig.node_id == alternate.id)
            )
            alternate_config.config = {
                **alternate_config.config,
                "share_address": "ru.example.test",
                "alternate_profile_links": True,
            }
            vmess_link = "vmess://" + base64.b64encode(
                json.dumps(
                    {"add": "ru.example.test", "port": 443, "net": "ws"}
                ).encode()
            ).decode().rstrip("=")
            links = [
                "vless://key@ru.example.test:443?type=tcp&security=reality#reality",
                "vless://key@ru.example.test:443?type=tcp&security=reality#REALITY-2",
                "hysteria2://key@ru.example.test:443?sni=example.test#hy2-current",
                "vless://key@ru.example.test:443?type=xhttp&security=reality#xhttp-current",
                "vless://key@ru.example.test:443?type=ws&security=tls#ws",
                "trojan://key@ru.example.test:443?type=grpc&security=tls#grpc",
                vmess_link,
                "tuic://key:secret@ru.example.test:8444?sni=example.test#tuic-current",
                "wireguard://key@ru.example.test:51820#wireguard",
                "vless://key@other.example.test:443?type=tcp#unrelated",
            ]
            with patch("app.api.routes.client.ThreeXUIClient") as panel:
                panel.return_value.get_client_links = AsyncMock(return_value=links)
                profiles = await alternate_client_profiles(db, initial.client, source_config)
            self.assertEqual(9, len(profiles))
            self.assertEqual(
                [
                    "🇷🇺 Россия (Reality)",
                    "🇷🇺 Россия (Reality 2)",
                    "🇷🇺 Россия (Hysteria2)",
                    "🇷🇺 Россия (xHTTP)",
                    "🇷🇺 Россия (VLESS WebSocket)",
                    "🇷🇺 Россия (Trojan gRPC)",
                    "🇷🇺 Россия (VMess WebSocket)",
                    "🇷🇺 Россия (TUIC)",
                    "🇷🇺 Россия (WireGuard)",
                ],
                [profile["name"] for profile in profiles],
            )
            initial.client.node_id = alternate.id
            with patch("app.api.routes.client.ThreeXUIClient") as panel:
                panel.return_value.get_client_links = AsyncMock(return_value=links)
                own_profiles = await alternate_client_profiles(db, initial.client, alternate_config)
            self.assertEqual(
                [
                    "🇷🇺 Россия (Reality 2)",
                    "🇷🇺 Россия (Hysteria2)",
                    "🇷🇺 Россия (xHTTP)",
                    "🇷🇺 Россия (VLESS WebSocket)",
                    "🇷🇺 Россия (Trojan gRPC)",
                    "🇷🇺 Россия (VMess WebSocket)",
                    "🇷🇺 Россия (TUIC)",
                    "🇷🇺 Россия (WireGuard)",
                ],
                [profile["name"] for profile in own_profiles],
            )

    async def test_reconciliation_preserves_shared_alternate_profile(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            source_config = await db.scalar(
                select(VPNNodeConfig).where(
                    VPNNodeConfig.node_id == self.node_id,
                    VPNNodeConfig.protocol == "vless",
                )
            )
            alternate = VPNNode(
                name="node-ru",
                provider="test",
                region="RU|Россия",
                hostname="ru.example.test",
                ip_address="203.0.113.11",
                status="active",
                capacity=100,
            )
            db.add(alternate)
            await db.flush()
            db.add(
                VPNNodeConfig(
                    node_id=alternate.id,
                    protocol="vless",
                    config={
                        "api_address": "https://master.example/base",
                        "inbound_tag": "10",
                        "host": "ru.example.test",
                        "port": 8443,
                    },
                )
            )
            source_config.config = {
                **source_config.config,
                "alternate_node_ids": [alternate.id],
            }
            await db.commit()

            report = await reconcile_node(db, alternate.id, panel_factory=FakePanel)
            email = f"vpn-{initial.client.id}"
            panel = FakePanel.users["https://master.example/base"]
            self.assertEqual(1, report.restored)
            self.assertEqual(initial.client.client_uuid, panel[email].client_uuid)
            self.assertIn("10", panel[email].inbound_ids)
            self.assertIn("vless-reality", panel[email].inbound_ids)

            source_config.config = {
                **source_config.config,
                "alternate_node_ids": [],
            }
            await db.commit()
            report = await reconcile_node(db, alternate.id, panel_factory=FakePanel)

            self.assertEqual(1, report.removed)
            self.assertNotIn("10", panel[email].inbound_ids)
            self.assertIn("vless-reality", panel[email].inbound_ids)

    async def test_renew_commit_failure_restores_previous_panel_client(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            previous_email = f"vpn-{initial.client.id}"
            plan = await db.get(Plan, self.plan_id)
            result = await renew_paid_subscription(
                db,
                subscription=initial.subscription,
                plan=plan,
                node_id=self.node_id,
                client_type="amnezia",
                flow="xtls-rprx-vision",
                fingerprint="chrome",
                panel_factory=FakePanel,
            )
            new_email = f"vpn-{result.client.id}"
            db.commit = AsyncMock(side_effect=RuntimeError("simulated commit failure"))

            with self.assertRaisesRegex(RuntimeError, "simulated commit failure"):
                await commit_provisioning(db, result)

            panel_users = FakePanel.users["https://master.example/base"]
            self.assertIn(previous_email, panel_users)
            self.assertEqual(previous_email, new_email)
            self.assertEqual(250 * 1024**3, panel_users[previous_email].total_gb)

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
                    panel_factory=FakePanel,
                )

            stored = await db.get(Payment, payment_id)
            self.assertEqual("failed", stored.status)
            self.assertEqual(
                2,
                await db.scalar(select(func.count()).select_from(PaymentEvent)),
            )

    async def test_refund_revokes_subscription_and_panel_access(self) -> None:
        async with self.session_factory() as db:
            payment = await create_payment(
                db,
                self.payment_data("telegram:refund"),
                provider="mock",
            )
            paid = await process_payment_event(
                db,
                provider="mock",
                event_id="refund-paid",
                provider_payment_id=payment.provider_payment_id,
                target_status="paid",
                payload={},
                panel_factory=FakePanel,
            )
            refunded = await process_payment_event(
                db,
                provider="mock",
                event_id="refund-completed",
                provider_payment_id=payment.provider_payment_id,
                target_status="refunded",
                payload={},
                panel_factory=FakePanel,
            )

            subscription = await db.get(Subscription, paid.subscription_id)
            client = (
                await db.execute(
                    select(VPNClient).where(
                        VPNClient.subscription_id == paid.subscription_id
                    )
                )
            ).scalar_one()
            self.assertEqual("refunded", refunded.status)
            self.assertEqual("cancelled", subscription.status)
            self.assertEqual("revoked", client.status)
            self.assertNotIn(
                f"vpn-{client.id}", FakePanel.users["https://master.example/base"]
            )

    async def test_panel_add_failure_rolls_back_paid_provisioning(self) -> None:
        async with self.session_factory() as db:
            payment = await create_payment(
                db,
                self.payment_data("telegram:callback-3"),
                provider="mock",
            )
            payment_id = payment.id
            FakePanel.fail_add = True
            with self.assertRaises(PaymentProvisioningError):
                await process_payment_event(
                    db,
                    provider="mock",
                    event_id="paid-with-xray-down",
                    provider_payment_id=payment.provider_payment_id,
                    target_status="paid",
                    payload={},
                    panel_factory=FakePanel,
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

    async def test_renew_preserves_client_and_extends_from_current_expiry(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            old_client_id = initial.client.id
            old_client_uuid = initial.client.client_uuid
            old_expiry = initial.subscription.expires_at

            with patch("app.api.routes.subscriptions.ThreeXUIClient", FakePanel):
                renewed = await renew_subscription(initial.subscription.id, db)

            clients = (
                await db.execute(
                    select(VPNClient)
                    .where(VPNClient.subscription_id == renewed.id)
                    .order_by(VPNClient.id)
                )
            ).scalars().all()
            self.assertEqual(1, len(clients))
            self.assertEqual(old_client_id, clients[0].id)
            self.assertEqual(old_client_uuid, clients[0].client_uuid)
            self.assertEqual("active", clients[0].status)
            self.assertEqual(
                (old_expiry + timedelta(days=30)).replace(tzinfo=None),
                renewed.expires_at.replace(tzinfo=None),
            )
            users = FakePanel.users["https://master.example/base"]
            self.assertIn(f"vpn-{old_client_id}", users)

    async def test_renew_restores_old_panel_client_when_update_fails(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            subscription_id = initial.subscription.id
            old_email = f"vpn-{initial.client.id}"
            old_expiry = initial.subscription.expires_at
            FakePanel.fail_update.add(old_email)

            with patch("app.api.routes.subscriptions.ThreeXUIClient", FakePanel):
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
            self.assertEqual(
                old_expiry.replace(tzinfo=None),
                clients[0].expires_at.replace(tzinfo=None),
            )
            self.assertEqual({old_email}, set(FakePanel.users["https://master.example/base"]))

    async def test_expiration_revokes_panel_and_database_client(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            initial.subscription.expires_at = expired_at
            initial.client.expires_at = expired_at
            await db.commit()

            count = await expire_subscriptions(db, panel_factory=FakePanel)
            self.assertEqual(1, count)
            await db.refresh(initial.subscription)
            await db.refresh(initial.client)
            self.assertEqual("expired", initial.subscription.status)
            self.assertEqual("revoked", initial.client.status)
            self.assertNotIn(
                f"vpn-{initial.client.id}",
                FakePanel.users["https://master.example/base"],
            )

    async def test_expiration_updates_master_and_database(self) -> None:
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
            self.assertIn(email, FakePanel.users["https://master.example/base"])

    async def test_reconciliation_restores_missing_and_removes_orphan(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            users = FakePanel.users["https://master.example/base"]
            users.clear()
            users["vpn-9999"] = SimpleNamespace(
                email="vpn-9999", inbound_ids={"vless-reality"}
            )
            users["vpn-test"] = SimpleNamespace(
                email="vpn-test", inbound_ids={"vless-reality"}
            )

            report = await reconcile_node(
                db,
                self.node_id,
                panel_factory=FakePanel,
            )

            self.assertEqual(1, report.restored)
            self.assertEqual(1, report.removed)
            self.assertIn(f"vpn-{initial.client.id}", users)
            self.assertIn("vpn-test", users)
            self.assertNotIn("vpn-9999", users)

    async def test_reconciliation_restores_missing_kit_memberships(self) -> None:
        async with self.session_factory() as db:
            initial = await self.provision(db)
            config = await db.scalar(
                select(VPNNodeConfig).where(VPNNodeConfig.node_id == self.node_id)
            )
            config.config = {**config.config, "kit_inbound_ids": [2, 3]}
            await db.commit()
            email = f"vpn-{initial.client.id}"
            user = FakePanel.users["https://master.example/base"][email]

            report = await reconcile_node(db, self.node_id, panel_factory=FakePanel)
            self.assertEqual((1, 0, 0), (report.present, report.restored, report.errors))
            self.assertTrue({"2", "3"}.issubset(user.inbound_ids))

            user.inbound_ids.remove("3")
            report = await reconcile_node(db, self.node_id, panel_factory=FakePanel)
            self.assertEqual(0, report.errors)
            self.assertIn("3", user.inbound_ids)
