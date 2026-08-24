from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.session import get_db
from app.schemas.vpn import (
    VPNNodeConfigCreate,
    VPNNodeConfigResponse,
)


router = APIRouter(
    prefix="/vpn",
    tags=["VPN"],
)


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
    # Проверяем существование ноды
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
