import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_access
from app.core.tokens import generate_scoped_token, token_hash
from app.db.models.audit import ActivationCode, ClientDevice
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.session import get_db
from app.schemas.client import (
    ActivationCodeCreate,
    ActivationCodeResponse,
    ClientProfileNode,
    ClientProfileResponse,
    DeviceActivate,
    DeviceTokenResponse,
)
from app.services.audit import write_audit
from app.services.node_health import node_accepts_clients
from app.core.config import settings


router = APIRouter(prefix="/v1/client", tags=["Client devices"])
device_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class DevicePrincipal:
    device_id: int
    user_id: int


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def require_device(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(device_bearer),
    db: AsyncSession = Depends(get_db),
) -> DevicePrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device token required")
    digest = token_hash(credentials.credentials)
    result = await db.execute(
        select(ClientDevice).where(
            ClientDevice.token_hash == digest,
            ClientDevice.status == "active",
        )
    )
    device = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if (
        device is None
        or not secrets.compare_digest(device.token_hash, digest)
        or aware(device.expires_at) <= now
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device token")
    device.last_seen_at = now
    await db.commit()
    request.state.principal = type(
        "DeviceAuditPrincipal",
        (),
        {"kind": "device", "name": f"device-{device.id}"},
    )()
    return DevicePrincipal(device_id=device.id, user_id=device.user_id)


@router.post(
    "/activation-codes",
    response_model=ActivationCodeResponse,
    dependencies=[Depends(require_api_access)],
)
async def create_activation_code(data: ActivationCodeCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.telegram_id == data.telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    now = datetime.now(timezone.utc)
    code = f"{secrets.randbelow(100_000_000):08d}"
    expires_at = now + timedelta(minutes=data.ttl_minutes)
    db.add(
        ActivationCode(
            user_id=user.id,
            code_hash=token_hash(code),
            code_prefix=code[:2],
            expires_at=expires_at,
        )
    )
    await db.commit()
    await write_audit(
        db,
        action="device.activation_code.create",
        result="success",
        actor_type="service",
        resource_type="user",
        resource_id=user.id,
        details={"expires_at": expires_at.isoformat()},
    )
    return ActivationCodeResponse(code=code, expires_at=expires_at)


@router.post("/activate", response_model=DeviceTokenResponse)
async def activate_device(data: DeviceActivate, db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ActivationCode)
        .where(ActivationCode.code_hash == token_hash(data.code))
        .with_for_update()
    )
    activation = result.scalar_one_or_none()
    if activation is None or activation.used_at is not None or aware(activation.expires_at) <= now:
        raise HTTPException(status_code=400, detail="Activation code is invalid or expired")
    token = generate_scoped_token("device", activation.user_id)
    expires_at = now + timedelta(days=90)
    device = ClientDevice(
        user_id=activation.user_id,
        name=data.name,
        platform=data.platform,
        token_hash=token_hash(token),
        token_prefix=token.split(".", 1)[0],
        status="active",
        expires_at=expires_at,
        last_seen_at=now,
    )
    db.add(device)
    await db.flush()
    activation.used_at = now
    activation.device_id = device.id
    await db.commit()
    await write_audit(
        db,
        action="device.activate",
        result="success",
        actor_type="device",
        actor_id=f"device-{device.id}",
        resource_type="client_device",
        resource_id=device.id,
        details={"platform": device.platform},
    )
    return DeviceTokenResponse(device_id=device.id, access_token=token, expires_at=expires_at)


def build_client_uri(client: VPNClient, node: VPNNode, config: dict) -> str:
    host = config.get("host") or node.hostname or node.ip_address
    params = {
        "type": config.get("type", "tcp"),
        "security": config.get("security", "none"),
        "encryption": "none",
        "fp": client.fingerprint or config.get("fp", "chrome"),
    }
    for key in ("sni", "pbk", "sid", "alpn", "path", "serviceName"):
        if config.get(key) is not None:
            params[key] = config[key]
    if client.flow:
        params["flow"] = client.flow
    return f"vless://{client.client_uuid}@{host}:{config.get('port', 443)}?{urlencode(params)}#VPN-{client.id}"


@router.get("/profile", response_model=ClientProfileResponse)
async def client_profile(
    principal: DevicePrincipal = Depends(require_device),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    subscription_result = await db.execute(
        select(Subscription)
        .where(
            Subscription.user_id == principal.user_id,
            Subscription.status == "active",
            Subscription.expires_at > now,
        )
        .order_by(Subscription.id.desc())
    )
    subscription = subscription_result.scalars().first()
    if subscription is None:
        raise HTTPException(status_code=403, detail="No active subscription")
    result = await db.execute(
        select(VPNClient, VPNNode, VPNNodeConfig)
        .join(VPNNode, VPNNode.id == VPNClient.node_id)
        .join(
            VPNNodeConfig,
            (VPNNodeConfig.node_id == VPNClient.node_id)
            & (VPNNodeConfig.protocol == VPNClient.protocol),
        )
        .where(
            VPNClient.subscription_id == subscription.id,
            VPNClient.status == "active",
            VPNClient.expires_at > now,
        )
    )
    nodes = []
    sensitive_configs = []
    for client, node, node_config in result.all():
        uri = build_client_uri(client, node, node_config.config)
        nodes.append(
            ClientProfileNode(
                node_id=node.id,
                name=node.name,
                region=node.region,
                available=node_accepts_clients(
                    node, management_mode=settings.xray_management_mode
                ),
                latency_ms=node.latency_ms,
                protocol=client.protocol,
                config=uri,
            )
        )
        sensitive_configs.append({"node_id": node.id, "vpn_uri": uri})
    await write_audit(
        db,
        action="device.profile.read",
        result="success",
        actor_type="device",
        actor_id=f"device-{principal.device_id}",
        resource_type="subscription",
        resource_id=subscription.id,
        details={"nodes": len(nodes)},
        sensitive_details={"configs": sensitive_configs},
    )
    return ClientProfileResponse(
        device_id=principal.device_id,
        user_id=principal.user_id,
        subscription_id=subscription.id,
        expires_at=subscription.expires_at,
        nodes=nodes,
    )


@router.post("/refresh", response_model=DeviceTokenResponse)
async def refresh_device_token(
    principal: DevicePrincipal = Depends(require_device),
    db: AsyncSession = Depends(get_db),
):
    device = await db.get(ClientDevice, principal.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    now = datetime.now(timezone.utc)
    token = generate_scoped_token("device", device.user_id)
    device.token_hash = token_hash(token)
    device.token_prefix = token.split(".", 1)[0]
    device.expires_at = now + timedelta(days=90)
    await db.commit()
    return DeviceTokenResponse(
        device_id=device.id,
        access_token=token,
        expires_at=device.expires_at,
    )
