from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class VPNNodeCreate(BaseModel):
    name: str
    provider: str
    region: str | None = None
    hostname: str | None = None
    ip_address: str
    capacity: int = 100


class VPNNodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str
    region: str | None
    hostname: str | None
    ip_address: str
    status: str
    capacity: int
    created_at: datetime
    updated_at: datetime


class VPNNodeConfigCreate(BaseModel):
    protocol: str
    config: dict[str, Any]


class VPNNodeConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    node_id: int
    protocol: str
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# =========================================================
# VPN Clients
# =========================================================

class VPNClientCreate(BaseModel):
    user_id: int
    subscription_id: int
    node_id: int
    protocol: str = "vless"


class VPNClientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    subscription_id: int
    node_id: int
    protocol: str
    client_uuid: str
    status: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class VPNClientConfigResponse(BaseModel):
    client_id: int
    protocol: str
    config: str
    expires_at: datetime
