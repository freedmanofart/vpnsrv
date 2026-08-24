from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.session import get_db
from app.schemas.user import UserCreate, UserResponse
from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.core.security import require_api_access

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

    db.add(user)

    await db.commit()
    await db.refresh(user)

    return user


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
