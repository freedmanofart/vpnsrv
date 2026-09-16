from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.session import get_db
from app.schemas.user import UserCreate, UserResponse
from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.core.security import require_api_access
from app.core.config import settings
from app.db.models.referral_reward import ReferralReward

router = APIRouter(
    prefix="/users",
    tags=["Users"],
    dependencies=[Depends(require_api_access)],
)


@router.post(
    "",
    response_model=UserResponse,
)
async def create_user(
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where(
            User.telegram_id == data.telegram_id
        )
    )

    existing = result.scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=409,
            detail="User already exists",
        )

    user = User(
        telegram_id=data.telegram_id,
        username=data.username,
        first_name=data.first_name,
        last_name=data.last_name,
    )
    if data.referred_by_telegram_id and data.referred_by_telegram_id != data.telegram_id:
        referrer = await db.scalar(select(User).where(User.telegram_id == data.referred_by_telegram_id))
        if referrer is not None:
            user.referred_by_user_id = referrer.id

    db.add(user)

    await db.commit()
    await db.refresh(user)

    return user


@router.get("/{telegram_id}/referral")
async def referral_stats(telegram_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    invited = await db.scalar(select(func.count(User.id)).where(User.referred_by_user_id == user.id)) or 0
    paid = await db.scalar(select(func.count(ReferralReward.id)).where(ReferralReward.inviter_id == user.id)) or 0
    username = settings.bot_username.strip().lstrip("@")
    return {
        "invited": int(invited),
        "paid": int(paid),
        "pending": max(0, int(invited) - int(paid)),
        "reward_days": int(paid) * 7,
        "link": f"https://t.me/{username}?start=ref_{user.id}",
    }


@router.get(
    "/{telegram_id}",
    response_model=UserResponse,
)
async def get_user(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
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

    return user
