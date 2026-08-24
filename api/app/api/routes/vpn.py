from datetime import datetime, timezone
from urllib.parse import urlencode
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

from app.services.xray import XrayClient, XrayError, XrayUserNotFound
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
        .order_by(VPNNode.id)
    )

    return result.scalars().all()


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

    node = VPNNode(
        name=data.name,
        provider=data.provider,
        region=data.region,
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
    if settings.xray_management_mode == "agent":
        return VPNNodeHealthResponse(
            node_id=node_id,
            status=node.health_status,
            xray_users=None,
            error=None if node.health_status == "online" else "Waiting for node-agent status",
        )
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
        return VPNNodeHealthResponse(node_id=node_id, status="offline", error="Xray management address not configured")
    started = time.perf_counter()
    try:
        users = await XrayClient(address=api_address).get_users(config.config.get("inbound_tag", "vless-reality"))
        node.health_status = "online"
        node.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        node.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
        return VPNNodeHealthResponse(node_id=node_id, status="online", xray_users=len(users))
    except XrayError as exc:
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
    if settings.xray_management_mode == "agent":
        raise HTTPException(
            status_code=409,
            detail="Reconciliation is performed automatically by the node-agent",
        )
    try:
        report = await reconcile_node(db, node_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except XrayError as exc:
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
        raise HTTPException(
            status_code=409,
            detail=f"{protocol} configuration already exists for this node",
        )

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

    if node.status != "active":
        raise HTTPException(
            status_code=400,
            detail="VPN node is not active",
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
        client_uuid=str(uuid4()),
        status="active",
        expires_at=subscription.expires_at,
    )

    db.add(client)

    await db.flush()

    # -----------------------------------------------------
    # Add client to Xray only on the single-node test stand. In agent mode the
    # committed database row is the desired state consumed by the node agent.
    # -----------------------------------------------------

    if protocol == "vless" and settings.xray_management_mode == "direct":
        xray = XrayClient(address=node_config.config.get("api_address"))

        try:
            await xray.add_vless_user(
                inbound_tag=node_config.config.get(
                    "inbound_tag",
                    "vless-reality",
                ),
                client_uuid=client.client_uuid,
                email=f"vpn-{client.id}",
                flow=client.flow,
            )
        except XrayError:
            await db.rollback()
            raise HTTPException(
                status_code=502,
                detail="Failed to add VPN client to Xray",
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
            detail="VPN node has no Xray management address",
        )

    # -----------------------------------------------------
    # Remove client from Xray
    # -----------------------------------------------------

    if client.protocol == "vless" and settings.xray_management_mode == "direct":
        xray = XrayClient(address=node_config.config.get("api_address"))

        try:
            await xray.remove_vless_user(
                inbound_tag=node_config.config.get(
                    "inbound_tag",
                    "vless-reality",
                ),
                email=f"vpn-{client.id}",
            )
        except XrayUserNotFound:
            pass
        except XrayError:
            raise HTTPException(
                status_code=502,
                detail="Failed to remove VPN client from Xray",
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

    params = {
        "type": config.get("type", "tcp"),
        "security": config.get("security", "none"),
        "encryption": "none",
    }

    for key in (
        "sni",
        "pbk",
        "sid",
        "alpn",
        "path",
        "serviceName",
    ):
        value = config.get(key)

        if value is not None:
            params[key] = value

    params["fp"] = client.fingerprint or config.get("fp", "chrome")
    if client.flow:
        params["flow"] = client.flow

    # HTTP Host для транспорта, если он отдельно задан
    if config.get("host_header"):
        params["host"] = config["host_header"]

    query = urlencode(params)

    vless_url = client.config_override or (
        f"vless://{client.client_uuid}"
        f"@{host}:{port}"
        f"?{query}"
        f"#VPN-{client.id}"
    )

    return VPNClientConfigResponse(
        client_id=client.id,
        protocol=client.protocol,
        config=vless_url,
        expires_at=client.expires_at,
    )
