from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SubscriptionCreate(BaseModel):
    user_id: int
    plan_id: int


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
