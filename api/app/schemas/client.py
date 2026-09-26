from datetime import datetime

from pydantic import BaseModel, Field


class ActivationCodeCreate(BaseModel):
    telegram_id: int
    ttl_minutes: int = Field(default=10, ge=1, le=60)


class ActivationCodeResponse(BaseModel):
    code: str
    device_name: str
    expires_at: datetime


class DeviceActivate(BaseModel):
    code: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    name: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=64)


class TrialActivate(BaseModel):
    install_id: str = Field(min_length=36, max_length=36)
    name: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=64)


class DeviceTokenResponse(BaseModel):
    device_id: int
    access_token: str
    expires_at: datetime


class ClientProfileNode(BaseModel):
    profile_id: str
    node_id: int
    name: str
    region: str | None
    available: bool
    latency_ms: float | None
    protocol: str
    config: str
    upload_bytes: int = 0
    download_bytes: int = 0


class ClientProfileUsage(BaseModel):
    upload_bytes: int = 0
    download_bytes: int = 0
    total_bytes: int | None = None
    remaining_bytes: int | None = None


class ClientProfileLink(BaseModel):
    id: str
    title: str
    url: str
    icon: str


class ClientProviderInfo(BaseModel):
    name: str
    support_url: str
    cabinet_url: str


class ClientProfileResponse(BaseModel):
    device_id: int
    user_id: int
    subscription_id: int
    expires_at: datetime
    provider: ClientProviderInfo
    provider_name: str
    plan_name: str
    announcement: str
    announcement_url: str | None = None
    usage: ClientProfileUsage
    links: list[ClientProfileLink]
    updated_at: datetime
    nodes: list[ClientProfileNode]
