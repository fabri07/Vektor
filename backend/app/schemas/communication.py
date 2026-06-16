"""Pydantic schemas para los endpoints de comunicación."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Channel = Literal["email", "whatsapp"]


class ChannelStatus(BaseModel):
    channel: Channel
    available: bool


class SendMessageRequest(BaseModel):
    channel: Channel
    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1)


class CommunicationLogResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    recipient_type: str
    recipient_id: UUID
    channel: str
    subject: str | None
    body: str
    status: str
    error: str | None
    created_at: datetime
