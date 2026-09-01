from datetime import datetime, timezone
from uuid import uuid4
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.session import get_db

from app.services.threexui import ThreeXUIClient, ThreeXUIError, ThreeXUIClientNotFound
from app.core.security import require_api_access
from app.core.config import settings

from app.schemas.vpn import (
    VPNClientConfigResponse,
    VPNClientCreate,
    VPNClientResponse,
    VPNNodeConfigCreate,
    VPNNodeConfigResponse,
    VPNNodeCreate,
    VPNNodeUpdate,
    VPNNodeResponse,
    VPNNodeHealthResponse,
    VPNNodeReconciliationResponse,
)
from app.services.reconciliation import reconcile_node
from app.services.node_health import effective_node_health, node_accepts_clients
from app.services.country import country_from_ip
from app.services.vless import build_vless_url

router = APIRouter(
    prefix="/vpn",
    tags=["VPN"],
    dependencies=[Depends(require_api_access)],
)


# =========================================================
# VPN Nodes
# =========================================================

@router.get(
    "/nodes",
    response_model=list[VPNNodeResponse],
)
async def get_nodes(
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(VPNNode)
        .where(VPNNode.status != "disabled")
        .order_by(VPNNode.id)
    )

    nodes = result.scalars().all()
    return [
        VPNNodeResponse.model_validate(node).model_copy(
            update={
                "health_status": effective_node_health(
                    node.health_status,
                    node.last_seen_at,
                    management_mode="threexui",
                )
            }
        )
        for node in nodes
    ]


@router.post(
    "/nodes",
    response_model=VPNNodeResponse,
)
async def create_node(
    data: VPNNodeCreate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(VPNNode).where(
            VPNNode.name == data.name
        )
    )

    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="VPN node already exists",
        )

    try:
        region = data.region or await country_from_ip(data.ip_address)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    node = VPNNode(
        name=data.name,
        provider=data.provider,
        region=region,
        hostname=data.hostname,
        ip_address=data.ip_address,
        capacity=data.capacity,
    )

    db.add(node)

    await db.commit()
    await db.refresh(node)

    return node


@router.patch("/nodes/{node_id}", response_model=VPNNodeResponse)
async def update_node(node_id: int, data: VPNNodeUpdate, db: AsyncSession = Depends(get_db)):
    node = await db.get(VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="VPN node not found")
    values = data.model_dump(exclude_unset=True)
    if "status" in values and values["status"] not in {"active", "offline", "maintenance", "draining", "disabled"}:
        raise HTTPException(status_code=400, detail="Unsupported node status")
    if "ip_address" in values and "region" not in values:
        try:
            values["region"] = await country_from_ip(values["ip_address"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    for key, value in values.items():
        setattr(node, key, value)
    await db.commit()
    await db.refresh(node)
    return node


@router.get("/nodes/{node_id}/health", response_model=VPNNodeHealthResponse)
async def check_node_health(node_id: int, db: AsyncSession = Depends(get_db)):
    node = await db.get(VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="VPN node not found")
    result = await db.execute(select(VPNNodeConfig).where(VPNNodeConfig.node_id == node_id, VPNNodeConfig.protocol == "vless"))
    config = result.scalar_one_or_none()
    if not config:
        node.health_status = "offline"
        node.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
        return VPNNodeHealthResponse(node_id=node_id, status="offline", error="VLESS configuration not found")
    api_address = config.config.get("api_address")
    if not api_address:
        node.health_status = "offline"
        node.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
        return VPNNodeHealthResponse(node_id=node_id, status="offline", error="3x-ui master URL not configured")
    started = time.perf_counter()
    try:
        users = await ThreeXUIClient(address=api_address).get_users(config.config.get("inbound_tag", "vless-reality"))
        node.health_status = "online"
        node.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        node.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
        return VPNNodeHealthResponse(node_id=node_id, status="online", xray_users=len(users))
    except ThreeXUIError as exc:
        node.health_status = "offline"
        node.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        node.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
        return VPNNodeHealthResponse(node_id=node_id, status="offline", error=str(exc))


@router.post(
    "/nodes/{node_id}/reconcile",
    response_model=VPNNodeReconciliationResponse,
)
async def reconcile_vpn_node(node_id: int, db: AsyncSession = Depends(get_db)):
    try:
        report = await reconcile_node(db, node_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ThreeXUIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return VPNNodeReconciliationResponse(**report.__dict__)


# =========================================================
# VPN Node Configs
# =========================================================

@router.post(
    "/nodes/{node_id}/configs",
    response_model=VPNNodeConfigResponse,
)
async def create_node_config(
    node_id: int,
    data: VPNNodeConfigCreate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(VPNNode).where(
            VPNNode.id == node_id
        )
    )

    node = result.scalar_one_or_none()

    if not node:
        raise HTTPException(
            status_code=404,
            detail="VPN node not found",
        )

    protocol = data.protocol.lower()

    if protocol not in {"vless", "amnezia"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported protocol",
        )

    # -----------------------------------------------------
    # Validate protocol configuration
    # -----------------------------------------------------

    if protocol == "vless":
        security = data.config.get("security", "none")

        api_address = str(data.config.get("api_address", ""))
        if api_address.startswith(("http://", "https://")):
            try:
                inbound_id = int(data.config.get("inbound_tag", ""))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="3x-ui configuration requires numeric inbound_tag",
                ) from exc
            if inbound_id <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="3x-ui inbound ID must be positive",
                )

        if security == "reality":
            required_fields = [
                "api_address",
                "host",
                "port",
                "type",
                "sni",
                "fp",
                "pbk",
                "sid",
            ]

            missing_fields = [
                field
                for field in required_fields
                if data.config.get(field) in (None, "")
            ]

            if missing_fields:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Invalid VLESS Reality configuration",
                        "missing_fields": missing_fields,
                    },
                )

            if not isinstance(data.config["port"], int):
                raise HTTPException(
                    status_code=400,
                    detail="VLESS port must be an integer",
                )
    result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == node_id,
            VPNNodeConfig.protocol == protocol,
        )
    )

    existing = result.scalar_one_or_none()

    if existing:
        existing.config = data.config
        await db.commit()
        await db.refresh(existing)
        return existing

    config = VPNNodeConfig(
        node_id=node.id,
        protocol=protocol,
        config=data.config,
    )

    db.add(config)

    await db.commit()
    await db.refresh(config)

    return config


@router.get(
    "/nodes/{node_id}/configs",
    response_model=list[VPNNodeConfigResponse],
)
async def get_node_configs(
    node_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(VPNNode).where(
            VPNNode.id == node_id
        )
    )

    node = result.scalar_one_or_none()

    if not node:
        raise HTTPException(
            status_code=404,
            detail="VPN node not found",
        )

    result = await db.execute(
        select(VPNNodeConfig)
        .where(
            VPNNodeConfig.node_id == node_id
        )
        .order_by(VPNNodeConfig.id)
    )

    return result.scalars().all()


# =========================================================
# VPN Clients
# =========================================================

@router.post(
    "/clients",
    response_model=VPNClientResponse,
)
async def create_vpn_client(
    data: VPNClientCreate,
    db: AsyncSession = Depends(get_db),
):


    protocol = data.protocol.lower()

    if protocol != "vless":
        raise HTTPException(
            status_code=400,
            detail="Only vless is supported",
        )

    # -----------------------------------------------------
    # User
    # -----------------------------------------------------

    result = await db.execute(
        select(User).where(
            User.id == data.user_id
        )
    )

    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )


    # -----------------------------------------------------
    # Subscription
    # -----------------------------------------------------

    result = await db.execute(
        select(Subscription).where(
            Subscription.id == data.subscription_id
        )
    )

    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=404,
            detail="Subscription not found",
        )

    if subscription.user_id != data.user_id:
        raise HTTPException(
            status_code=400,
            detail="Subscription does not belong to this user",
        )

    if subscription.status != "active":
        raise HTTPException(
            status_code=400,
            detail="Subscription is not active",
        )

    if subscription.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail="Subscription has expired",
        )

    plan = await db.get(Plan, subscription.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    # -----------------------------------------------------
    # VPN Node
    # -----------------------------------------------------

    result = await db.execute(
        select(VPNNode).where(
            VPNNode.id == data.node_id
        )
    )

    node = result.scalar_one_or_none()

    if not node:
        raise HTTPException(
            status_code=404,
            detail="VPN node not found",
        )

    if not node_accepts_clients(node, management_mode="threexui"):
        raise HTTPException(
            status_code=503,
            detail="VPN node is not available",
        )

    # -----------------------------------------------------
    # Node configuration
    # -----------------------------------------------------

    result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == data.node_id,
            VPNNodeConfig.protocol == protocol,
        )
    )

    node_config = result.scalar_one_or_none()

    if not node_config:
        raise HTTPException(
            status_code=404,
            detail="VPN node configuration not found",
        )
    if not node_config.config.get("api_address"):
        raise HTTPException(
            status_code=503,
            detail="VPN node has no Xray management address",
        )

    # -----------------------------------------------------
    # Existing client
    # -----------------------------------------------------

    result = await db.execute(
        select(VPNClient).where(
            VPNClient.subscription_id == subscription.id,
            VPNClient.status == "active",
        )
    )

    existing_client = result.scalar_one_or_none()

    if existing_client:
        raise HTTPException(
            status_code=409,
            detail="Active VPN client already exists for this subscription",
        )

    # -----------------------------------------------------
    # Create client
    # -----------------------------------------------------

    client = VPNClient(
        user_id=data.user_id,
        subscription_id=data.subscription_id,
        node_id=data.node_id,
        protocol=protocol,
        client_type="universal",
        flow="",
        fingerprint="chrome",
        max_connections=plan.max_connections,
        client_uuid=str(uuid4()),
        status="active",
        expires_at=subscription.expires_at,
    )

    db.add(client)

    await db.flush()

    # -----------------------------------------------------
    # Add the client to the inbound managed by the 3x-ui master.
    # -----------------------------------------------------

    if protocol == "vless":
        xray = ThreeXUIClient(address=node_config.config.get("api_address"))

        try:
            await xray.add_vless_user(
                inbound_tag=node_config.config.get(
                    "inbound_tag",
                    "vless-reality",
                ),
                client_uuid=client.client_uuid,
                email=f"vpn-{client.id}",
                flow=client.flow,
                expiry_time=int(subscription.expires_at.timestamp() * 1000),
                telegram_id=user.telegram_id,
                limit_ip=client.max_connections,
            )
        except ThreeXUIError:
            await db.rollback()
            raise HTTPException(
                status_code=502,
                detail="Failed to add VPN client to 3x-ui",
            )

    await db.commit()
    await db.refresh(client)

    return client

@router.get(
    "/clients/{client_id}",
    response_model=VPNClientResponse,
)
async def get_vpn_client(
    client_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(VPNClient).where(
            VPNClient.id == client_id
        )
    )

    client = result.scalar_one_or_none()

    if not client:
        raise HTTPException(
            status_code=404,
            detail="VPN client not found",
        )

    return client

# =========================================================
# Get VPN Client Config
# =========================================================

@router.delete(
    "/clients/{client_id}",
    response_model=VPNClientResponse,
)
async def revoke_vpn_client(
    client_id: int,
    db: AsyncSession = Depends(get_db),
):
    # -----------------------------------------------------
    # Client
    # -----------------------------------------------------

    result = await db.execute(
        select(VPNClient).where(
            VPNClient.id == client_id
        )
    )

    client = result.scalar_one_or_none()

    if not client:
        raise HTTPException(
            status_code=404,
            detail="VPN client not found",
        )

    # -----------------------------------------------------
    # Already revoked
    # -----------------------------------------------------

    if client.status != "active":
        raise HTTPException(
            status_code=400,
            detail="VPN client is not active",
        )

    # -----------------------------------------------------
    # Node configuration
    # -----------------------------------------------------

    result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == client.node_id,
            VPNNodeConfig.protocol == client.protocol,
        )
    )

    node_config = result.scalar_one_or_none()

    if not node_config:
        raise HTTPException(
            status_code=404,
            detail="VPN node configuration not found",
        )
    if not node_config.config.get("api_address"):
        raise HTTPException(
            status_code=503,
            detail="VPN node has no 3x-ui master URL",
        )

    # -----------------------------------------------------
    # Remove client through the 3x-ui master.
    # -----------------------------------------------------

    if client.protocol == "vless":
        xray = ThreeXUIClient(address=node_config.config.get("api_address"))

        try:
            await xray.remove_vless_user(
                inbound_tag=node_config.config.get(
                    "inbound_tag",
                    "vless-reality",
                ),
                email=f"vpn-{client.id}",
            )
        except ThreeXUIClientNotFound:
            pass
        except ThreeXUIError:
            raise HTTPException(
                status_code=502,
                detail="Failed to remove VPN client from 3x-ui",
            )

    # -----------------------------------------------------
    # Revoke client in DB
    # -----------------------------------------------------

    client.status = "revoked"
    client.revoked_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(client)

    return client



@router.get(
    "/clients/{client_id}/config",
    response_model=VPNClientConfigResponse,
)
async def get_vpn_client_config(
    client_id: int,
    db: AsyncSession = Depends(get_db),
):
    # -----------------------------------------------------
    # Client
    # -----------------------------------------------------

    result = await db.execute(
        select(VPNClient).where(
            VPNClient.id == client_id
        )
    )

    client = result.scalar_one_or_none()

    if not client:
        raise HTTPException(
            status_code=404,
            detail="VPN client not found",
        )

    # -----------------------------------------------------
    # Client status
    # -----------------------------------------------------

    if client.status != "active":
        raise HTTPException(
            status_code=403,
            detail="VPN client is not active",
        )

    if client.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=403,
            detail="VPN client has expired",
        )

    # -----------------------------------------------------
    # Node
    # -----------------------------------------------------

    result = await db.execute(
        select(VPNNode).where(
            VPNNode.id == client.node_id
        )
    )

    node = result.scalar_one_or_none()

    if not node:
        raise HTTPException(
            status_code=404,
            detail="VPN node not found",
        )

    # -----------------------------------------------------
    # Node config
    # -----------------------------------------------------

    result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == client.node_id,
            VPNNodeConfig.protocol == client.protocol,
        )
    )

    node_config = result.scalar_one_or_none()

    if not node_config:
        raise HTTPException(
            status_code=404,
            detail="VPN node configuration not found",
        )

    config = node_config.config

    # -----------------------------------------------------
    # VLESS URL
    # -----------------------------------------------------

    host = (
        config.get("host")
        or node.hostname
        or node.ip_address
    )

    port = config.get("port", 443)

    link_config = dict(config)
    link_config["fp"] = config.get("fp") or client.fingerprint or "firefox"
    if client.flow:
        link_config["flow"] = client.flow

    vless_url = client.config_override or (
        build_vless_url(
            uuid=client.client_uuid,
            host=host,
            port=port,
            config=link_config,
            remark=f"vpn-{client.id}",
        )
    )

    return VPNClientConfigResponse(
        client_id=client.id,
        protocol=client.protocol,
        config=vless_url,
        expires_at=client.expires_at,
    )
