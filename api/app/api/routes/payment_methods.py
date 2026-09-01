from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, require_api_access
from app.db.models.payment_method import PaymentMethod
from app.db.session import get_db


router = APIRouter(prefix="/payment-methods", tags=["Payment methods"])


class PaymentMethodData(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=255)
    url: str | None = Field(default=None, max_length=2048)
    sort_order: int = Field(default=100, ge=0, le=10000)
    is_active: bool = True


class PaymentMethodUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    url: str | None = Field(default=None, max_length=2048)
    sort_order: int | None = Field(default=None, ge=0, le=10000)
    is_active: bool | None = None


class PaymentMethodResponse(PaymentMethodData):
    model_config = ConfigDict(from_attributes=True)
    id: int


@router.get("", response_model=list[PaymentMethodResponse], dependencies=[Depends(require_api_access)])
async def list_payment_methods(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).order_by(PaymentMethod.sort_order, PaymentMethod.id)
    )
    return result.scalars().all()


@router.post("", response_model=PaymentMethodResponse, dependencies=[Depends(require_admin)])
async def create_payment_method(data: PaymentMethodData, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(PaymentMethod).where(PaymentMethod.code == data.code))
    if existing:
        raise HTTPException(status_code=409, detail="Payment method already exists")
    item = PaymentMethod(**data.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@router.patch("/{method_id}", response_model=PaymentMethodResponse, dependencies=[Depends(require_admin)])
async def update_payment_method(method_id: int, data: PaymentMethodUpdate, db: AsyncSession = Depends(get_db)):
    item = await db.get(PaymentMethod, method_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Payment method not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/{method_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_payment_method(method_id: int, db: AsyncSession = Depends(get_db)):
    item = await db.get(PaymentMethod, method_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Payment method not found")
    await db.delete(item)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
