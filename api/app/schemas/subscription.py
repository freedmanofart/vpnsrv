from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SubscriptionCreate(BaseModel):
    user_id: int
    plan_id: int
    node_id: int | None = None
    client_type: str = "universal"
    flow: str = ""
    fingerprint: str = "chrome"


class VPNClientRotate(BaseModel):
    node_id: int
    client_type: str = "universal"
    flow: str = ""
    fingerprint: str = "chrome"


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    plan_id: int
    status: str
    starts_at: datetime
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
