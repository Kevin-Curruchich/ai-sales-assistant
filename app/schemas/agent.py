import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AgentThreadCreate(BaseModel):
    title: Optional[str] = None


class AgentThreadRename(BaseModel):
    title: str


class AgentThreadResponse(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
