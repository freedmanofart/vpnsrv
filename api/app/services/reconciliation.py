import re
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.services.xray import (
    XrayClient,
    XrayError,
    XrayUserAlreadyExists,
    XrayUserNotFound,
)


MANAGED_EMAIL = re.compile(r"^vpn-(\d+)$")


@dataclass
class ReconciliationReport:
    node_id: int
    expected: int = 0
    present: int = 0
    restored: int = 0
    removed: int = 0
    errors: int = 0


async def reconcile_node(
    db: AsyncSession,
    node_id: int,
    *,
    xray_factory: Callable[..., XrayClient] = XrayClient,
) -> ReconciliationReport:
    node = await db.get(VPNNode, node_id)
    if node is None:
        raise ValueError("VPN node not found")

    config_result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == node_id,
            VPNNodeConfig.protocol == "vless",
        )
    )
    config = config_result.scalar_one_or_none()
    if config is None:
        raise ValueError("VPN node configuration not found")
    api_address = config.config.get("api_address")
    if not api_address:
        raise ValueError("VPN node has no Xray management address")

    client_result = await db.execute(
        select(VPNClient).where(
            VPNClient.node_id == node_id,
            VPNClient.protocol == "vless",
            VPNClient.status == "active",
        )
    )
    clients = client_result.scalars().all()
    expected = {f"vpn-{client.id}": client for client in clients}
    report = ReconciliationReport(node_id=node_id, expected=len(expected))

    inbound_tag = config.config.get("inbound_tag", "vless-reality")
    xray = xray_factory(address=api_address)
    users = await xray.get_users(inbound_tag)
    actual = {user.email for user in users if user.email}
    report.present = len(set(expected) & actual)

    for email, client in expected.items():
        if email in actual:
            continue
        try:
            await xray.add_vless_user(
                inbound_tag=inbound_tag,
                client_uuid=client.client_uuid,
                email=email,
                flow=client.flow,
            )
            report.restored += 1
        except XrayUserAlreadyExists:
            report.present += 1
        except XrayError:
            report.errors += 1

    for email in actual:
        if not MANAGED_EMAIL.fullmatch(email) or email in expected:
            continue
        try:
            await xray.remove_vless_user(inbound_tag=inbound_tag, email=email)
            report.removed += 1
        except XrayUserNotFound:
            pass
        except XrayError:
            report.errors += 1

    return report


async def reconcile_all_nodes(
    db: AsyncSession,
    *,
    xray_factory: Callable[..., XrayClient] = XrayClient,
) -> list[ReconciliationReport]:
    result = await db.execute(
        select(VPNNode.id).where(VPNNode.status.in_(("active", "draining")))
    )
    reports: list[ReconciliationReport] = []
    for node_id in result.scalars():
        try:
            reports.append(
                await reconcile_node(db, node_id, xray_factory=xray_factory)
            )
        except (ValueError, XrayError):
            reports.append(ReconciliationReport(node_id=node_id, errors=1))
    return reports
