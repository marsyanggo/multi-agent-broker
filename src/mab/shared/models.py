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


class AgentSnapshot(Agent):
    last_heartbeat_age_seconds: float
    is_stale: bool


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


class Channel(BaseModel):
    """A named group-broadcast topic. Agents join and posts go to all
    subscribed members via WS push (plus persistent DB history)."""

    id: str
    name: str
    description: str = ""
    created_by: str
    created_at: datetime


class ChannelMessage(BaseModel):
    """A message posted to a channel. Distinct from direct Message (1:1):
    these have channel_id and broadcast to all subscribers, no to_agent."""

    id: str
    channel_id: str
    from_agent: str
    content: str
    content_type: ContentType = "text/plain"
    created_at: datetime


class Context(BaseModel):
    """A persistent named document any agent can read.

    Use cases:
      - Pin a project spec / style guide once, reference in many task
        descriptions.
      - Auto-promote upstream task results into named handoff docs for
        downstream tasks (via the optional task_id link).
      - Long-form context that's awkward to inline in task descriptions.

    Names aren't unique — multiple drafts can share a name, references
    are always by id. `list_contexts(name=...)` filters by name.
    """

    id: str
    name: str
    content: str
    content_type: ContentType = "text/markdown"
    created_by: str
    task_id: str | None = None
    created_at: datetime
    updated_at: datetime


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
    required_all: list[str] = Field(default_factory=list)
    required_any: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
