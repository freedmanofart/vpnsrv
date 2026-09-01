from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PlanCreate(BaseModel):
    code: str
    name: str
    duration_days: int
    max_connections: int = Field(default=1, ge=0, le=100)
    traffic_limit_gb: int = Field(default=0, ge=0, le=100000)
    price: Decimal
    currency: str = "RUB"
    is_public: bool = True


class PlanUpdate(BaseModel):
    name: str | None = None
    duration_days: int | None = None
    max_connections: int | None = Field(default=None, ge=0, le=100)
    traffic_limit_gb: int | None = Field(default=None, ge=0, le=100000)
    price: Decimal | None = None
    currency: str | None = None
    is_active: bool | None = None
    is_public: bool | None = None


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    duration_days: int
    max_connections: int
    traffic_limit_gb: int
    price: Decimal
    currency: str
    is_active: bool
    is_public: bool
    created_at: datetime
