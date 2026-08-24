from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class PlanCreate(BaseModel):
    code: str
    name: str
    duration_days: int
    price: Decimal
    currency: str = "RUB"


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
