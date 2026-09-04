from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PlanCreate(BaseModel):
    code: str
    name: str
    duration_days: int
    max_connections: int = Field(default=1, ge=0, le=100)
    traffic_limit_gb: int = Field(default=0, ge=0, le=100000)
    package_id: int | None = None
    price: Decimal
    currency: str = "RUB"
    is_public: bool = True


class PlanUpdate(BaseModel):
    name: str | None = None
    duration_days: int | None = None
    max_connections: int | None = Field(default=None, ge=0, le=100)
    traffic_limit_gb: int | None = Field(default=None, ge=0, le=100000)
    package_id: int | None = None
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
    package_id: int | None
    price: Decimal
    currency: str
    is_active: bool
    is_public: bool
    created_at: datetime


class PlanPackageCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=255)
    description: str = ""
    max_connections: int = Field(default=0, ge=0, le=100)
    traffic_limit_gb: int = Field(default=0, ge=0, le=100000)
    sort_order: int = 100
    is_active: bool = True


class PlanPackageUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    max_connections: int | None = Field(default=None, ge=0, le=100)
    traffic_limit_gb: int | None = Field(default=None, ge=0, le=100000)
    sort_order: int | None = None
    is_active: bool | None = None


class PlanPackageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str
    max_connections: int
    traffic_limit_gb: int
    sort_order: int
    is_active: bool
    created_at: datetime
