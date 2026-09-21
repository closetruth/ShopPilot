from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1)
    mode: Literal["chat", "extract"] = "chat"


class ExtractRequest(BaseModel):
    text: str = Field(min_length=1)
    session_id: str | None = None


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str
    role: str
    content: str
    seq: int
    created_at: datetime
