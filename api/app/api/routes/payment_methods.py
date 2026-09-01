import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import Response as FastAPIResponse
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
    has_image: bool = False


class PaymentMethodImage(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(pattern=r"^image/(png|jpeg|webp)$")
    data_base64: str = Field(min_length=4, max_length=6_000_000)


def _response(item: PaymentMethod) -> dict:
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "url": item.url,
        "sort_order": item.sort_order,
        "is_active": item.is_active,
        "has_image": item.image_data is not None,
    }


@router.get("", response_model=list[PaymentMethodResponse], dependencies=[Depends(require_api_access)])
async def list_payment_methods(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).order_by(PaymentMethod.sort_order, PaymentMethod.id)
    )
    return [_response(item) for item in result.scalars().all()]


@router.post("", response_model=PaymentMethodResponse, dependencies=[Depends(require_admin)])
async def create_payment_method(data: PaymentMethodData, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(PaymentMethod).where(PaymentMethod.code == data.code))
    if existing:
        raise HTTPException(status_code=409, detail="Payment method already exists")
    item = PaymentMethod(**data.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _response(item)


@router.patch("/{method_id}", response_model=PaymentMethodResponse, dependencies=[Depends(require_admin)])
async def update_payment_method(method_id: int, data: PaymentMethodUpdate, db: AsyncSession = Depends(get_db)):
    item = await db.get(PaymentMethod, method_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Payment method not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    await db.commit()
    await db.refresh(item)
    return _response(item)


@router.put("/{method_id}/image", response_model=PaymentMethodResponse, dependencies=[Depends(require_admin)])
async def upload_payment_method_image(method_id: int, data: PaymentMethodImage, db: AsyncSession = Depends(get_db)):
    item = await db.get(PaymentMethod, method_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Payment method not found")
    try:
        image = base64.b64decode(data.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 image") from exc
    if not image or len(image) > 4_000_000:
        raise HTTPException(status_code=413, detail="Image must be between 1 byte and 4 MB")
    item.image_data = image
    item.image_mime_type = data.mime_type
    item.image_filename = data.filename
    await db.commit()
    await db.refresh(item)
    return _response(item)


@router.get("/{method_id}/image", dependencies=[Depends(require_api_access)])
async def get_payment_method_image(method_id: int, db: AsyncSession = Depends(get_db)):
    item = await db.get(PaymentMethod, method_id)
    if item is None or item.image_data is None:
        raise HTTPException(status_code=404, detail="Payment method image not found")
    return FastAPIResponse(
        content=item.image_data,
        media_type=item.image_mime_type or "image/png",
        headers={"Content-Disposition": f'inline; filename="{item.image_filename or "qr.png"}"'},
    )


@router.delete("/{method_id}/image", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_payment_method_image(method_id: int, db: AsyncSession = Depends(get_db)):
    item = await db.get(PaymentMethod, method_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Payment method not found")
    item.image_data = None
    item.image_mime_type = None
    item.image_filename = None
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{method_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_payment_method(method_id: int, db: AsyncSession = Depends(get_db)):
    item = await db.get(PaymentMethod, method_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Payment method not found")
    await db.delete(item)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
