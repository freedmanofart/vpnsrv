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
from app.services.threexui import ThreeXUIClient, ThreeXUIError, ThreeXUIClientNotFound
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
class ProvisioningResult:
    subscription: Subscription
    client: VPNClient
    xray: ThreeXUIClient | None
    inbound_tag: str

    async def compensate(self) -> None:
        if self.xray is None:
            return
        try:
            await self.xray.remove_vless_user(
                inbound_tag=self.inbound_tag,
                email=f"vpn-{self.client.id}",
            )
        except (ThreeXUIError, ThreeXUIClientNotFound):
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
