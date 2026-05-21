from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mab.broker.auth import get_current_agent, get_db
from mab.broker.config import settings
from mab.broker.db import Database
from mab.shared.models import Agent, AgentSnapshot, AgentStatus, utc_now

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


# How many heartbeat intervals can pass before we consider an agent's
# liveness stale. We don't flip status here — clients decide what to do.
_STALE_MULTIPLIER = 3


class UpdateAgentRequest(BaseModel):
    status: AgentStatus | None = None
    capabilities: list[str] | None = None


class MatchAgentsRequest(BaseModel):
    required_all: list[str] = []
    required_any: list[str] = []
    available_only: bool = False
    status: AgentStatus | None = "online"


def _snapshot(agent: Agent) -> AgentSnapshot:
    age = (utc_now() - agent.last_heartbeat).total_seconds()
    threshold = settings.heartbeat_interval_seconds * _STALE_MULTIPLIER
    return AgentSnapshot(
        **agent.model_dump(),
        last_heartbeat_age_seconds=age,
        is_stale=age > threshold,
    )


@router.get("", response_model=list[AgentSnapshot])
async def list_agents(
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
    status: AgentStatus | None = None,
) -> list[AgentSnapshot]:
    agents = await db.list_agents(status=status)
    return [_snapshot(a) for a in agents]


@router.post("/match", response_model=list[AgentSnapshot])
async def match_agents(
    body: MatchAgentsRequest,
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
) -> list[AgentSnapshot]:
    agents = await db.find_matching_agents(
        required_all=body.required_all,
        required_any=body.required_any,
        status=body.status,
        available_only=body.available_only,
    )
    return [_snapshot(a) for a in agents]


@router.get("/me", response_model=AgentSnapshot)
async def get_me(
    me: Annotated[Agent, Depends(get_current_agent)],
) -> AgentSnapshot:
    return _snapshot(me)


@router.patch("/me", response_model=AgentSnapshot)
async def update_me(
    body: UpdateAgentRequest,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> AgentSnapshot:
    if body.status is not None:
        await db.set_agent_status(me.id, body.status)
    if body.capabilities is not None:
        await db.update_agent_capabilities(me.id, body.capabilities)
    updated = await db.get_agent(me.id)
    assert updated is not None
    return _snapshot(updated)


@router.get("/{agent_id}", response_model=AgentSnapshot)
async def get_agent(
    agent_id: str,
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
) -> AgentSnapshot:
    agent = await db.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _snapshot(agent)
