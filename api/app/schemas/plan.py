from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class PlanCreate(BaseModel):
    code: str
    name: str
    duration_days: int
    price: Decimal
    currency: str = "RUB"


class PlanUpdate(BaseModel):
    name: str | None = None
    duration_days: int | None = None
    price: Decimal | None = None
    currency: str | None = None
    is_active: bool | None = None


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    duration_days: int
    price: Decimal
    currency: str
    is_active: bool
    created_at: datetime
