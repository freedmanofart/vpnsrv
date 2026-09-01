from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.services.threexui import (
    ThreeXUIClient,
    ThreeXUIClientAlreadyExists,
    ThreeXUIClientNotFound,
    ThreeXUIError,
)
from app.core.config import settings


class ProvisioningError(Exception):
    pass


class ProvisioningNotFound(ProvisioningError):
    pass


class ProvisioningConflict(ProvisioningError):
    pass


class ProvisioningInvalid(ProvisioningError):
    pass


class ProvisioningThreeXUIError(ProvisioningError):
    pass


@dataclass
class PanelClientSnapshot:
    xray: ThreeXUIClient
    inbound_tag: str
    client_uuid: str
    email: str
    flow: str
    expiry_time: int
    telegram_id: int
    limit_ip: int
    total_gb: int

    async def restore(self) -> None:
        await self.xray.add_vless_user(
            inbound_tag=self.inbound_tag,
            client_uuid=self.client_uuid,
            email=self.email,
            flow=self.flow,
            expiry_time=self.expiry_time,
            telegram_id=self.telegram_id,
            limit_ip=self.limit_ip,
            total_gb=self.total_gb,
        )


@dataclass
class ProvisioningResult:
    subscription: Subscription
    client: VPNClient
    xray: ThreeXUIClient | None
    inbound_tag: str
    client_email: str | None = None
    previous_client: PanelClientSnapshot | None = None

    async def compensate(self) -> None:
        if self.xray is None:
            return
        try:
            await self.xray.remove_vless_user(
                inbound_tag=self.inbound_tag,
                email=self.client_email or f"vpn-{self.client.id}",
            )
        except (ThreeXUIError, ThreeXUIClientNotFound):
            pass
        if self.previous_client is not None:
            try:
                await self.previous_client.restore()
            except (ThreeXUIError, ThreeXUIClientAlreadyExists):
                pass


def _validate_profile(client_type: str, flow: str, fingerprint: str) -> None:
    if client_type not in {"amnezia", "universal"}:
        raise ProvisioningInvalid("Unsupported client type")
    if flow not in {"", "xtls-rprx-vision"}:
        raise ProvisioningInvalid("Unsupported VLESS flow")
    if fingerprint not in {"chrome", "firefox", "safari", "randomized"}:
        raise ProvisioningInvalid("Unsupported fingerprint")


async def provision_subscription(
    db: AsyncSession,
    *,
    user_id: int,
    plan_id: int,
    node_id: int,
    client_type: str,
    flow: str,
    fingerprint: str,
    panel_factory: Callable[..., ThreeXUIClient] = ThreeXUIClient,
) -> ProvisioningResult:
    _validate_profile(client_type, flow, fingerprint)
    now = datetime.now(timezone.utc)

    user_result = await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise ProvisioningNotFound("User not found")

    plan = await db.get(Plan, plan_id)
    if plan is None or not plan.is_active:
        raise ProvisioningNotFound("Plan not found")

    existing_result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.status == "active",
            Subscription.expires_at > now,
        )
    )
    if existing_result.scalar_one_or_none() is not None:
        raise ProvisioningConflict("Active subscription already exists")

    node = await db.get(VPNNode, node_id)
    if node is None:
        raise ProvisioningNotFound("VPN node not found")
    if node.status != "active":
        raise ProvisioningInvalid("VPN node is not active")

    config_result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == node_id,
            VPNNodeConfig.protocol == "vless",
        )
    )
    node_config = config_result.scalar_one_or_none()
    if node_config is None:
        raise ProvisioningNotFound("VPN node configuration not found")
    api_address = node_config.config.get("api_address")
    if not api_address:
        raise ProvisioningInvalid("VPN node has no Xray management address")

    expires_at = now + timedelta(days=plan.duration_days)
    subscription = Subscription(
        user_id=user_id,
        plan_id=plan_id,
        status="active",
        starts_at=now,
        expires_at=expires_at,
    )
    db.add(subscription)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ProvisioningConflict("Active subscription already exists") from exc

    client = VPNClient(
        user_id=user_id,
        subscription_id=subscription.id,
        node_id=node_id,
        protocol="vless",
        client_type=client_type,
        flow=flow,
        fingerprint=fingerprint,
        max_connections=plan.max_connections,
        traffic_limit_gb=plan.traffic_limit_gb,
        client_uuid=str(uuid4()),
        status="active",
        expires_at=expires_at,
    )
    db.add(client)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ProvisioningConflict(
            "Active VPN client already exists for this subscription"
        ) from exc

    inbound_tag = node_config.config.get("inbound_tag", "vless-reality")
    xray = panel_factory(address=api_address)
    try:
        await xray.add_vless_user(
            inbound_tag=inbound_tag,
            client_uuid=client.client_uuid,
            email=f"vpn-{client.id}",
            flow=client.flow,
            expiry_time=int(subscription.expires_at.timestamp() * 1000),
            telegram_id=user.telegram_id,
            limit_ip=client.max_connections,
            total_gb=client.traffic_limit_gb * 1024 * 1024 * 1024,
        )
    except ThreeXUIError as exc:
        await db.rollback()
        raise ProvisioningThreeXUIError(f"Failed to add VPN client to 3x-ui: {exc}") from exc

    return ProvisioningResult(
        subscription=subscription,
        client=client,
        xray=xray,
        inbound_tag=inbound_tag,
        client_email=f"vpn-{client.id}",
    )


async def renew_paid_subscription(
    db: AsyncSession,
    *,
    subscription: Subscription,
    plan: Plan,
    node_id: int,
    client_type: str,
    flow: str,
    fingerprint: str,
    panel_factory: Callable[..., ThreeXUIClient] = ThreeXUIClient,
) -> ProvisioningResult:
    """Replace the active client and extend from the current expiry for a paid order."""
    _validate_profile(client_type, flow, fingerprint)
    now = datetime.now(timezone.utc)
    user = await db.get(User, subscription.user_id)
    node = await db.get(VPNNode, node_id)
    if user is None or node is None:
        raise ProvisioningNotFound("User or VPN node not found")
    if node.status != "active":
        raise ProvisioningInvalid("VPN node is not active")
    node_config = await db.scalar(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == node_id,
            VPNNodeConfig.protocol == "vless",
        )
    )
    if node_config is None or not node_config.config.get("api_address"):
        raise ProvisioningNotFound("VPN node configuration not found")
    previous = (
        await db.execute(
            select(VPNClient)
            .where(
                VPNClient.subscription_id == subscription.id,
                VPNClient.status == "active",
            )
            .order_by(VPNClient.id.desc())
            .with_for_update()
        )
    ).scalars().first()
    previous_snapshot: PanelClientSnapshot | None = None
    if previous is not None:
        previous_config = await db.scalar(
            select(VPNNodeConfig).where(
                VPNNodeConfig.node_id == previous.node_id,
                VPNNodeConfig.protocol == previous.protocol,
            )
        )
        if previous_config is None or not previous_config.config.get("api_address"):
            raise ProvisioningNotFound("Previous VPN node configuration not found")
        previous_inbound_tag = previous_config.config.get("inbound_tag", "vless-reality")
        previous_xray = panel_factory(address=previous_config.config["api_address"])
        previous_expiry = previous.expires_at
        if previous_expiry.tzinfo is None:
            previous_expiry = previous_expiry.replace(tzinfo=timezone.utc)
        previous_snapshot = PanelClientSnapshot(
            xray=previous_xray,
            inbound_tag=previous_inbound_tag,
            client_uuid=previous.client_uuid,
            email=f"vpn-{previous.id}",
            flow=previous.flow,
            expiry_time=int(previous_expiry.timestamp() * 1000),
            telegram_id=user.telegram_id,
            limit_ip=previous.max_connections,
            total_gb=previous.traffic_limit_gb * 1024 * 1024 * 1024,
        )
    current_expiry = subscription.expires_at
    if current_expiry.tzinfo is None:
        current_expiry = current_expiry.replace(tzinfo=timezone.utc)
    expires_at = max(current_expiry, now) + timedelta(days=plan.duration_days)
    client = VPNClient(
        user_id=user.id,
        subscription_id=subscription.id,
        node_id=node.id,
        protocol="vless",
        client_type=client_type,
        flow=flow,
        fingerprint=fingerprint,
        max_connections=plan.max_connections,
        traffic_limit_gb=plan.traffic_limit_gb,
        client_uuid=str(uuid4()),
        status="provisioning",
        expires_at=expires_at,
    )
    db.add(client)
    await db.flush()
    inbound_tag = node_config.config.get("inbound_tag", "vless-reality")
    xray = panel_factory(address=node_config.config["api_address"])
    try:
        await xray.add_vless_user(
            inbound_tag=inbound_tag,
            client_uuid=client.client_uuid,
            email=f"vpn-{client.id}",
            flow=client.flow,
            expiry_time=int(expires_at.timestamp() * 1000),
            telegram_id=user.telegram_id,
            limit_ip=client.max_connections,
            total_gb=client.traffic_limit_gb * 1024 * 1024 * 1024,
        )
        if previous is not None:
            try:
                await previous_snapshot.xray.remove_vless_user(
                    inbound_tag=previous_snapshot.inbound_tag,
                    email=previous_snapshot.email,
                )
            except ThreeXUIClientNotFound:
                pass
    except ThreeXUIError as exc:
        try:
            await xray.remove_vless_user(inbound_tag=inbound_tag, email=f"vpn-{client.id}")
        except (ThreeXUIError, ThreeXUIClientNotFound):
            pass
        await db.rollback()
        raise ProvisioningThreeXUIError(f"Failed to renew VPN client in 3x-ui: {exc}") from exc
    if previous is not None:
        previous.status = "revoked"
        previous.revoked_at = now
    client.status = "active"
    subscription.plan_id = plan.id
    subscription.status = "active"
    subscription.expires_at = expires_at
    return ProvisioningResult(
        subscription=subscription,
        client=client,
        xray=xray,
        inbound_tag=inbound_tag,
        client_email=f"vpn-{client.id}",
        previous_client=previous_snapshot,
    )


async def commit_provisioning(
    db: AsyncSession,
    result: ProvisioningResult,
) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await result.compensate()
        raise
