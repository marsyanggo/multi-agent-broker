from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mab.broker.auth import get_current_agent, get_db
from mab.broker.db import Database
from mab.broker.websocket import WebSocketHub, get_hub
from mab.shared.capabilities import matches_capabilities
from mab.shared.models import Agent, Task, TaskPriority, TaskStatus

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    assigned_to: str | None = None
    priority: TaskPriority = "normal"
    required_all: list[str] = []
    required_any: list[str] = []


class UpdateTaskRequest(BaseModel):
    status: TaskStatus | None = None
    result: str | None = None
    note: str | None = None


@router.post("", response_model=Task, status_code=201)
async def create_task(
    body: CreateTaskRequest,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
    hub: Annotated[WebSocketHub, Depends(get_hub)],
) -> Task:
    if body.assigned_to is not None:
        assignee = await db.get_agent(body.assigned_to)
        if assignee is None:
            raise HTTPException(status_code=404, detail="assigned_to agent not found")
        if not matches_capabilities(
            assignee.capabilities, body.required_all, body.required_any
        ):
            raise HTTPException(
                status_code=400,
                detail="assigned_to agent lacks required capabilities",
            )
    task = await db.create_task(
        title=body.title,
        description=body.description,
        created_by=me.id,
        assigned_to=body.assigned_to,
        priority=body.priority,
        required_all=body.required_all,
        required_any=body.required_any,
    )
    extra_targets: list[str] = []
    if task.assigned_to is None:
        matched = await db.find_matching_agents(
            required_all=task.required_all,
            required_any=task.required_any,
            status="online",
        )
        extra_targets = [a.id for a in matched]
    await hub.emit_task_event("created", task, extra_targets=extra_targets)
    return task


@router.get("", response_model=list[Task])
async def list_tasks(
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
    status: TaskStatus | None = None,
    assigned_to: str | None = None,
    created_by: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[Task]:
    return await db.list_tasks(
        status=status,
        assigned_to=assigned_to,
        created_by=created_by,
        limit=limit,
    )


@router.get("/{task_id}", response_model=Task)
async def get_task(
    task_id: str,
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
) -> Task:
    task = await db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.post("/{task_id}/claim", response_model=Task)
async def claim_task(
    task_id: str,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
    hub: Annotated[WebSocketHub, Depends(get_hub)],
) -> Task:
    existing = await db.get_task(task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not matches_capabilities(
        me.capabilities, existing.required_all, existing.required_any
    ):
        raise HTTPException(
            status_code=403,
            detail="agent lacks required capabilities for this task",
        )
    claimed = await db.claim_task(task_id=task_id, agent_id=me.id)
    if claimed is None:
        # Lost the race: re-read for accurate status in error message.
        current = await db.get_task(task_id)
        status = current.status if current else "missing"
        raise HTTPException(status_code=409, detail=f"task already {status}")
    await db.set_current_task(me.id, claimed.id)
    await hub.emit_task_event("claimed", claimed)
    return claimed


@router.patch("/{task_id}", response_model=Task)
async def update_task(
    task_id: str,
    body: UpdateTaskRequest,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
    hub: Annotated[WebSocketHub, Depends(get_hub)],
) -> Task:
    task = await db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.assigned_to != me.id:
        raise HTTPException(status_code=403, detail="not the assignee")

    updated = await db.update_task(
        task_id,
        status=body.status,
        result=body.result,
        note=body.note,
    )
    assert updated is not None
    if updated.status in ("completed", "failed") and updated.assigned_to == me.id:
        await db.set_current_task(me.id, None)

    event_name = (
        "completed" if updated.status == "completed"
        else "failed" if updated.status == "failed"
        else "updated"
    )
    await hub.emit_task_event(event_name, updated)
    return updated
