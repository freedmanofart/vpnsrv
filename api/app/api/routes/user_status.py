from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.db.models.plan import Plan
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.session import get_db
from app.core.security import require_api_access
from app.services.threexui import ThreeXUIClient, ThreeXUIError


router = APIRouter(
    prefix="/users",
    tags=["Users"],
    dependencies=[Depends(require_api_access)],
)


@router.get("/{telegram_id}/vpn-status")
async def get_vpn_status(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    # -----------------------------------------------------
    # User
    # -----------------------------------------------------

    result = await db.execute(
        select(User).where(
            User.telegram_id == telegram_id
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
        select(Subscription)
        .where(
            Subscription.user_id == user.id
        )
        .order_by(Subscription.id.desc())
    )

    subscription = result.scalars().first()

    if not subscription:
        return {
            "user_id": user.id,
            "telegram_id": user.telegram_id,
            "subscription": None,
            "vpn_client": None,
        }

    # -----------------------------------------------------
    # VPN client
    # -----------------------------------------------------

    result = await db.execute(
        select(VPNClient)
        .where(
            VPNClient.subscription_id == subscription.id,
            VPNClient.status == "active",
        )
        .order_by(VPNClient.id.desc())
    )

    client = result.scalars().first()

    plan = await db.get(Plan, subscription.plan_id)
    traffic_used_bytes = None
    traffic_remaining_bytes = None
    if client:
        config_result = await db.execute(
            select(VPNNodeConfig).where(
                VPNNodeConfig.node_id == client.node_id,
                VPNNodeConfig.protocol == "vless",
            )
        )
        node_config = config_result.scalar_one_or_none()
        if node_config:
            try:
                traffic = await ThreeXUIClient(node_config.config.get("api_address")).get_client_traffic(
                    f"vpn-{client.id}"
                )
                traffic_used_bytes = int(traffic.get("up", 0)) + int(traffic.get("down", 0))
                total_bytes = int(traffic.get("total", 0))
                traffic_remaining_bytes = max(total_bytes - traffic_used_bytes, 0) if total_bytes else None
            except (ThreeXUIError, TypeError, ValueError):
                pass

    now = datetime.now(timezone.utc)

    expires_at = subscription.expires_at.replace(
        tzinfo=subscription.expires_at.tzinfo or timezone.utc
    )

    subscription_active = (
        subscription.status == "active"
        and expires_at > now
        and traffic_remaining_bytes != 0
    )

    return {
        "user_id": user.id,
        "telegram_id": user.telegram_id,
        "subscription": {
            "id": subscription.id,
            "status": (
                "active"
                if subscription_active
                else "expired"
            ),
            "starts_at": subscription.starts_at,
            "expires_at": subscription.expires_at,
            "plan_name": plan.name if plan else None,
            "days_remaining": max((expires_at - now).total_seconds() / 86400, 0),
        },
        "vpn_client": (
            {
                "id": client.id,
                "protocol": client.protocol,
                "client_type": client.client_type,
                "flow": client.flow,
                "fingerprint": client.fingerprint,
                "status": client.status,
                "expires_at": client.expires_at,
                "max_connections": client.max_connections,
                "traffic_limit_gb": client.traffic_limit_gb,
                "traffic_used_bytes": traffic_used_bytes,
                "traffic_remaining_bytes": traffic_remaining_bytes,
            }
            if client
            else None
        ),
    }
