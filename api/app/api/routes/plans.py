from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.plan import Plan
from app.db.models.payment import Payment
from app.db.models.subscription import Subscription
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
        max_connections=data.max_connections,
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


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    subscriptions = await db.scalar(
        select(func.count(Subscription.id)).where(Subscription.plan_id == plan_id)
    )
    payments = await db.scalar(
        select(func.count(Payment.id)).where(Payment.plan_id == plan_id)
    )
    if subscriptions or payments:
        raise HTTPException(
            status_code=409,
            detail=(
                "Тариф уже используется в подписках или платежах. "
                "Отключите его через «Изменить», чтобы сохранить историю."
            ),
        )

    await db.delete(plan)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
