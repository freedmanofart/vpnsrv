from datetime import datetime

from pydantic import BaseModel, Field


class ActivationCodeCreate(BaseModel):
    telegram_id: int
    ttl_minutes: int = Field(default=10, ge=1, le=60)


class ActivationCodeResponse(BaseModel):
    code: str
    expires_at: datetime


class DeviceActivate(BaseModel):
    code: str = Field(min_length=6, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=64)


class DeviceTokenResponse(BaseModel):
    device_id: int
    access_token: str
    expires_at: datetime


class ClientProfileNode(BaseModel):
    node_id: int
    name: str
    region: str | None
    available: bool
    latency_ms: float | None
    protocol: str
    config: str


class ClientProfileResponse(BaseModel):
    device_id: int
    user_id: int
    subscription_id: int
    expires_at: datetime
    nodes: list[ClientProfileNode]
