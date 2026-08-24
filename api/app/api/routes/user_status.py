from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.db.session import get_db


router = APIRouter(
    prefix="/users",
    tags=["Users"],
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

    now = datetime.now(timezone.utc)

    subscription_active = (
        subscription.status == "active"
        and subscription.expires_at > now
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
            }
            if client
            else None
        ),
    }
