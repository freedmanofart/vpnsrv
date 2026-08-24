import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin
from app.core.tokens import generate_scoped_token, token_hash
from app.db.models.audit import NodeAgentCredential
from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.session import get_db
from app.schemas.agent import (
    AgentCredentialResponse,
    DesiredClient,
    NodeDesiredStateResponse,
    NodeStatusReport,
)
from app.services.audit import write_audit


router = APIRouter(prefix="/agent/v1", tags=["Node agent"])
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class NodePrincipal:
    node_id: int
    credential_id: int


async def require_node_agent(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> NodePrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Node token required")
    digest = token_hash(credentials.credentials)
    result = await db.execute(
        select(NodeAgentCredential).where(
            NodeAgentCredential.token_hash == digest,
            NodeAgentCredential.status == "active",
        )
    )
    stored = result.scalar_one_or_none()
    if stored is None or not secrets.compare_digest(stored.token_hash, digest):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid node token")
    principal = NodePrincipal(node_id=stored.node_id, credential_id=stored.id)
    request.state.principal = type(
        "AgentAuditPrincipal",
        (),
        {"kind": "node-agent", "name": f"node-{stored.node_id}"},
    )()
    return principal


@router.post(
    "/credentials/{node_id}/rotate",
    response_model=AgentCredentialResponse,
    dependencies=[Depends(require_admin)],
)
async def rotate_agent_credential(node_id: int, db: AsyncSession = Depends(get_db)):
    node = await db.get(VPNNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="VPN node not found")
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(NodeAgentCredential).where(NodeAgentCredential.node_id == node_id)
    )
    credential = result.scalar_one_or_none()
    token = generate_scoped_token("node", node_id)
    digest = token_hash(token)
    prefix = token.split(".", 1)[0]
    if credential is None:
        credential = NodeAgentCredential(
            node_id=node_id,
            token_hash=digest,
            token_prefix=prefix,
            status="active",
        )
        db.add(credential)
    else:
        credential.token_hash = digest
        credential.token_prefix = prefix
        credential.status = "active"
        credential.rotated_at = now
    await db.commit()
    await write_audit(
        db,
        action="node.credential.rotate",
        result="success",
        actor_type="admin",
        resource_type="vpn_node",
        resource_id=node_id,
        node_id=node_id,
    )
    return AgentCredentialResponse(node_id=node_id, token=token, token_prefix=prefix)


@router.get("/state", response_model=NodeDesiredStateResponse)
async def desired_state(
    principal: NodePrincipal = Depends(require_node_agent),
    db: AsyncSession = Depends(get_db),
):
    config_result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == principal.node_id,
            VPNNodeConfig.protocol == "vless",
        )
    )
    config = config_result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="VLESS node configuration not found")
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(VPNClient)
        .join(Subscription, Subscription.id == VPNClient.subscription_id)
        .where(
            VPNClient.node_id == principal.node_id,
            VPNClient.status == "active",
            VPNClient.expires_at > now,
            Subscription.status == "active",
            Subscription.expires_at > now,
        )
        .order_by(VPNClient.id)
    )
    clients = result.scalars().all()
    version = f"{principal.node_id}:{max((client.id for client in clients), default=0)}:{len(clients)}"
    return NodeDesiredStateResponse(
        node_id=principal.node_id,
        version=version,
        inbound_tag=config.config.get("inbound_tag", "vless-reality"),
        clients=[
            DesiredClient(
                id=client.id,
                email=f"vpn-{client.id}",
                client_uuid=client.client_uuid,
                flow=client.flow,
                expires_at=client.expires_at,
            )
            for client in clients
        ],
    )


@router.post("/status")
async def report_status(
    data: NodeStatusReport,
    principal: NodePrincipal = Depends(require_node_agent),
    db: AsyncSession = Depends(get_db),
):
    if data.status not in {"online", "degraded", "offline"}:
        raise HTTPException(status_code=400, detail="Unsupported node health status")
    now = datetime.now(timezone.utc)
    node = await db.get(VPNNode, principal.node_id)
    credential = await db.get(NodeAgentCredential, principal.credential_id)
    if node is None or credential is None:
        raise HTTPException(status_code=404, detail="Node credential is no longer valid")
    node.health_status = data.status
    node.latency_ms = data.latency_ms
    node.active_connections = data.active_connections
    node.last_seen_at = now
    credential.last_seen_at = now
    if data.activities:
        client_ids = {item.client_id for item in data.activities}
        result = await db.execute(
            select(VPNClient).where(
                VPNClient.node_id == principal.node_id,
                VPNClient.id.in_(client_ids),
            )
        )
        clients = {client.id: client for client in result.scalars()}
        for item in data.activities:
            client = clients.get(item.client_id)
            if client is None:
                continue
            previous = client.last_connected_at
            if previous is not None and previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            if previous is None or item.connected_at > previous:
                client.last_connected_at = item.connected_at
                client.last_ip = item.ip_address
    await db.commit()
    await write_audit(
        db,
        action="node.status",
        result="failure" if data.errors else "success",
        actor_type="node-agent",
        actor_id=f"node-{principal.node_id}",
        resource_type="vpn_node",
        resource_id=principal.node_id,
        node_id=principal.node_id,
        details=data.model_dump(),
    )
    return {"accepted": True, "server_time": now}
