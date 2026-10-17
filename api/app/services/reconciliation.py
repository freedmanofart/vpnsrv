import re
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.services.provisioning import ProvisioningInvalid, _kit_inbound_ids
from app.services.threexui import (
    ThreeXUIClient,
    ThreeXUIError,
    ThreeXUIClientAlreadyExists,
    ThreeXUIClientNotFound,
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
    panel_factory: Callable[..., ThreeXUIClient] = ThreeXUIClient,
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
    try:
        kit_inbound_ids = _kit_inbound_ids(config)
    except ProvisioningInvalid as exc:
        raise ValueError(str(exc)) from exc

    shared_configs_result = await db.execute(
        select(VPNNodeConfig, VPNNode.status)
        .join(VPNNode, VPNNode.id == VPNNodeConfig.node_id)
        .where(VPNNodeConfig.protocol == "vless")
    )
    shared_node_ids: set[int] = set()
    alternate_node_ids: set[int] = set()
    for other_config, status in shared_configs_result.all():
        other = other_config.config or {}
        if other_config.node_id == node_id or other.get("api_address") != api_address:
            continue
        shared_node_ids.add(other_config.node_id)
        alternate_ids = other.get("alternate_node_ids", [])
        try:
            active_alternate_ids = {int(value) for value in alternate_ids}
        except (TypeError, ValueError):
            active_alternate_ids = set()
        if status in ("active", "draining") and node_id in active_alternate_ids:
            alternate_node_ids.add(other_config.node_id)

    managed_node_ids = {node_id, *shared_node_ids}
    client_result = await db.execute(
        select(VPNClient, Plan.code).join(
            Subscription, Subscription.id == VPNClient.subscription_id
        ).join(
            Plan, Plan.id == Subscription.plan_id
        ).where(
            VPNClient.node_id.in_(managed_node_ids),
            VPNClient.protocol == "vless",
            VPNClient.status == "active",
        )
    )
    clients = client_result.all()
    all_shared_clients = {f"vpn-{client.id}": client for client, _ in clients}
    expected = {}
    for client, plan_code in clients:
        if client.node_id == node_id or client.node_id in alternate_node_ids:
            expected[f"vpn-{client.id}"] = (
                client,
                plan_code,
                client.node_id != node_id,
            )
    report = ReconciliationReport(node_id=node_id, expected=len(expected))

    inbound_tag = config.config.get("inbound_tag", "vless-reality")
    xray = panel_factory(address=api_address)
    users = await xray.get_users(inbound_tag)
    actual = {user.email for user in users if user.email}
    report.present = len(set(expected) & actual)

    for email, (client, plan_code, is_alternate) in expected.items():
        if email in actual:
            if kit_inbound_ids:
                try:
                    await xray.attach_client(email, kit_inbound_ids)
                except ThreeXUIError:
                    report.errors += 1
            if not plan_code.startswith("mobile-trial-"):
                continue
            try:
                await xray.update_vless_user(
                    inbound_tag=inbound_tag,
                    client_uuid=client.client_uuid,
                    email=email,
                    flow=client.flow,
                    expiry_time=int(client.expires_at.timestamp() * 1000),
                    limit_ip=client.max_connections,
                    total_gb=client.traffic_limit_gb * 1024 * 1024 * 1024,
                )
            except ThreeXUIClientNotFound:
                # The panel changed between the list and update calls. The
                # next reconciliation pass will restore the missing client.
                report.present -= 1
                continue
            except ThreeXUIError:
                report.errors += 1
            continue
        try:
            if is_alternate:
                await xray.attach_client(
                    email,
                    [ThreeXUIClient._inbound_id(inbound_tag)],
                )
                if kit_inbound_ids:
                    await xray.attach_client(email, kit_inbound_ids)
            else:
                await xray.add_vless_user(
                    inbound_tag=inbound_tag,
                    client_uuid=client.client_uuid,
                    email=email,
                    flow=client.flow,
                    expiry_time=int(client.expires_at.timestamp() * 1000),
                    limit_ip=client.max_connections,
                    total_gb=client.traffic_limit_gb * 1024 * 1024 * 1024,
                )
                if kit_inbound_ids:
                    await xray.attach_client(email, kit_inbound_ids)
            report.restored += 1
        except ThreeXUIClientAlreadyExists:
            # The panel may not have returned this user in the list because of
            # a stale cache, while add still sees the existing UUID/email.
            # Update it immediately so a trial quota/expiry cannot remain
            # stale until the next reconciliation cycle.
            try:
                await xray.update_vless_user(
                    inbound_tag=inbound_tag,
                    client_uuid=client.client_uuid,
                    email=email,
                    flow=client.flow,
                    expiry_time=int(client.expires_at.timestamp() * 1000),
                    limit_ip=client.max_connections,
                    total_gb=client.traffic_limit_gb * 1024 * 1024 * 1024,
                )
                if kit_inbound_ids:
                    await xray.attach_client(email, kit_inbound_ids)
                report.present += 1
            except ThreeXUIError:
                report.errors += 1
        except ThreeXUIError:
            report.errors += 1

    for email in actual:
        if not MANAGED_EMAIL.fullmatch(email) or email in expected:
            continue
        try:
            if (
                email in all_shared_clients
                and all_shared_clients[email].node_id != node_id
            ):
                await xray.detach_client(
                    email,
                    [ThreeXUIClient._inbound_id(inbound_tag)],
                )
            else:
                await xray.remove_vless_user(inbound_tag=inbound_tag, email=email)
            report.removed += 1
        except ThreeXUIClientNotFound:
            pass
        except ThreeXUIError:
            report.errors += 1

    return report


async def reconcile_all_nodes(
    db: AsyncSession,
    *,
    panel_factory: Callable[..., ThreeXUIClient] = ThreeXUIClient,
) -> list[ReconciliationReport]:
    result = await db.execute(
        select(VPNNode.id).where(VPNNode.status.in_(("active", "draining")))
    )
    reports: list[ReconciliationReport] = []
    for node_id in result.scalars():
        try:
            reports.append(
                await reconcile_node(db, node_id, panel_factory=panel_factory)
            )
        except (ValueError, ThreeXUIError):
            reports.append(ReconciliationReport(node_id=node_id, errors=1))
    return reports
