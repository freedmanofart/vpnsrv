from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.plan import Plan
from app.db.session import get_db
from app.schemas.plan import PlanCreate, PlanResponse


router = APIRouter(
    prefix="/plans",
    tags=["Plans"],
)


@router.get(
    "",
    response_model=list[PlanResponse],
)
async def get_plans(
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Plan)
        .where(Plan.is_active.is_(True))
        .order_by(Plan.duration_days)
    )

    return result.scalars().all()


@router.post(
    "",
    response_model=PlanResponse,
)
async def create_plan(
    data: PlanCreate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Plan).where(
            Plan.code == data.code
        )
    )

    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="Plan already exists",
        )

    plan = Plan(
        code=data.code,
        name=data.name,
        duration_days=data.duration_days,
        price=data.price,
        currency=data.currency,
    )

    db.add(plan)

    await db.commit()
    await db.refresh(plan)

    return plan
