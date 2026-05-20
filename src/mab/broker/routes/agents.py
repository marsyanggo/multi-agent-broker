from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mab.broker.auth import get_current_agent, get_db
from mab.broker.db import Database
from mab.shared.models import Agent, AgentStatus

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


class UpdateAgentStatusRequest(BaseModel):
    status: AgentStatus


@router.get("", response_model=list[Agent])
async def list_agents(
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
    status: AgentStatus | None = None,
) -> list[Agent]:
    return await db.list_agents(status=status)


@router.get("/me", response_model=Agent)
async def get_me(
    me: Annotated[Agent, Depends(get_current_agent)],
) -> Agent:
    return me


@router.patch("/me", response_model=Agent)
async def update_me(
    body: UpdateAgentStatusRequest,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> Agent:
    await db.set_agent_status(me.id, body.status)
    updated = await db.get_agent(me.id)
    assert updated is not None
    return updated


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(
    agent_id: str,
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
) -> Agent:
    agent = await db.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent
