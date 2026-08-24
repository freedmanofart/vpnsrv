from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.plan import Plan
from app.db.session import get_db
from app.schemas.plan import PlanCreate, PlanResponse, PlanUpdate
from app.core.security import require_api_access


router = APIRouter(
    prefix="/plans",
    tags=["Plans"],
    dependencies=[Depends(require_api_access)],
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
        .where(Plan.is_active.is_(True), Plan.is_public.is_(True))
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
        is_public=data.is_public,
    )

    db.add(plan)

    await db.commit()
    await db.refresh(plan)

    return plan


@router.patch("/{plan_id}", response_model=PlanResponse)
async def update_plan(plan_id: int, data: PlanUpdate, db: AsyncSession = Depends(get_db)):
    plan = await db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    values = data.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(plan, key, value)
    await db.commit()
    await db.refresh(plan)
    return plan
