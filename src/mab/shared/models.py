from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

AgentStatus = Literal["online", "busy", "idle", "offline"]
TaskStatus = Literal[
    "pending",
    "assigned",
    "in_progress",
    "completed",
    "failed",
    "blocked",
]
TaskPriority = Literal["low", "normal", "high", "urgent"]
ContentType = Literal["text/plain", "text/markdown", "application/json"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def short_uuid() -> str:
    return uuid.uuid4().hex[:8]


class Agent(BaseModel):
    id: str
    name: str
    machine_id: str
    capabilities: list[str] = Field(default_factory=list)
    status: AgentStatus = "online"
    current_task: str | None = None
    registered_at: datetime
    last_heartbeat: datetime


class Message(BaseModel):
    id: str
    from_agent: str
    to_agent: str
    content: str
    content_type: ContentType = "text/plain"
    reply_to: str | None = None
    created_at: datetime
    delivered: bool = False
    delivered_at: datetime | None = None


class Task(BaseModel):
    id: str
    title: str
    description: str = ""
    created_by: str
    assigned_to: str | None = None
    status: TaskStatus = "pending"
    priority: TaskPriority = "normal"
    result: str | None = None
    notes: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
