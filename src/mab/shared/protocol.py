from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from mab.shared.models import Agent, Message, Task, utc_now

TaskEventName = Literal[
    "created", "claimed", "updated", "completed", "failed", "deleted"
]
AgentEventName = Literal["online", "offline"]


class MessagePayload(BaseModel):
    message: Message


class TaskEventPayload(BaseModel):
    event: TaskEventName
    task: Task


class AgentEventPayload(BaseModel):
    event: AgentEventName
    agent: Agent


class AckPayload(BaseModel):
    ack_id: str


class _EnvelopeBase(BaseModel):
    id: str
    from_agent: str | None = None
    to_agent: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)


class MessageEnvelope(_EnvelopeBase):
    type: Literal["message"] = "message"
    payload: MessagePayload


class TaskEventEnvelope(_EnvelopeBase):
    type: Literal["task_event"] = "task_event"
    payload: TaskEventPayload


class AgentEventEnvelope(_EnvelopeBase):
    type: Literal["agent_event"] = "agent_event"
    payload: AgentEventPayload


class AckEnvelope(_EnvelopeBase):
    type: Literal["ack"] = "ack"
    payload: AckPayload


AnyEnvelope = Union[
    MessageEnvelope,
    TaskEventEnvelope,
    AgentEventEnvelope,
    AckEnvelope,
]

Envelope = Annotated[AnyEnvelope, Field(discriminator="type")]

envelope_adapter: TypeAdapter[AnyEnvelope] = TypeAdapter(Envelope)
