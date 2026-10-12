import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict


class FollowUpPostpone(BaseModel):
    date: dt.date


class FollowUpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    product_id: uuid.UUID
    due_date: dt.date
    status: str
