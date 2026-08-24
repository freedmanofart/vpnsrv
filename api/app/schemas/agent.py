from datetime import datetime

from pydantic import BaseModel, Field


class AgentCredentialResponse(BaseModel):
    node_id: int
    token: str
    token_prefix: str


class DesiredClient(BaseModel):
    id: int
    email: str
    client_uuid: str
    flow: str
    expires_at: datetime


class NodeDesiredStateResponse(BaseModel):
    node_id: int
    version: str
    inbound_tag: str
    clients: list[DesiredClient]


class NodeStatusReport(BaseModel):
    status: str
    latency_ms: float | None = Field(default=None, ge=0)
    xray_users: int = Field(ge=0)
    active_connections: int = Field(default=0, ge=0)
    restored: int = Field(default=0, ge=0)
    removed: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list, max_length=50)
