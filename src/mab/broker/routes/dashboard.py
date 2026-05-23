from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mab.broker.auth import get_current_agent, get_db
from mab.broker.config import settings
from mab.broker.db import Database
from mab.shared.models import (
    Agent,
    AgentSnapshot,
    Channel,
    Context,
    Task,
    utc_now,
)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

_STALE_MULTIPLIER = 3


def _snapshot(agent: Agent) -> AgentSnapshot:
    age = (utc_now() - agent.last_heartbeat).total_seconds()
    threshold = settings.heartbeat_interval_seconds * _STALE_MULTIPLIER
    return AgentSnapshot(
        **agent.model_dump(),
        last_heartbeat_age_seconds=age,
        is_stale=age > threshold,
    )


class ChannelSummary(BaseModel):
    channel: Channel
    member_count: int
    message_count: int


class DashboardSnapshot(BaseModel):
    agents: list[AgentSnapshot]
    tasks: list[Task]
    channels: list[ChannelSummary]
    contexts: list[Context]
    server_time: str


@router.get("/snapshot", response_model=DashboardSnapshot)
async def snapshot(
    _me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> DashboardSnapshot:
    """Single-shot snapshot of broker state for dashboard initial render.

    Any authenticated agent can read. Dashboard polls this every ~1s as the
    fallback when WS push isn't connected; on WS reconnect the dashboard
    refetches once to catch up before resuming push-driven updates."""
    agents = await db.list_agents()
    tasks = await db.list_tasks()
    channels_raw = await db.list_channels()
    contexts = await db.list_contexts()

    channels: list[ChannelSummary] = []
    for ch in channels_raw:
        members = await db.list_channel_members(ch.id)
        msgs = await db.list_channel_messages(ch.id, limit=10_000)
        channels.append(
            ChannelSummary(
                channel=ch,
                member_count=len(members),
                message_count=len(msgs),
            )
        )

    return DashboardSnapshot(
        agents=[_snapshot(a) for a in agents],
        tasks=tasks,
        channels=channels,
        contexts=contexts,
        server_time=utc_now().isoformat(),
    )
