from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_type: str = Field(min_length=1, max_length=64)
    source_ip: IPvAnyAddress
    username: str = Field(min_length=1, max_length=128)


class Event(EventCreate):
    id: UUID
    created_at: datetime
