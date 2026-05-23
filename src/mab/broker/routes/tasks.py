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
    depends_on: list[str] = []


class UpdateTaskRequest(BaseModel):
    status: TaskStatus | None = None
    result: str | None = None
    note: str | None = None


async def _propagate_completion(
    completed: Task, db: Database, hub: WebSocketHub
) -> None:
    """When `completed` enters status=completed, unblock downstream tasks
    whose full dependency set is now satisfied. Emits task_event:created
    via filter-broadcast for each newly-unblocked downstream task."""
    candidates = await db.find_blocked_downstream(completed.id)
    for downstream in candidates:
        all_done = True
        for dep_id in downstream.depends_on:
            dep = await db.get_task(dep_id)
            if dep is None or dep.status != "completed":
                all_done = False
                break
        if not all_done:
            continue

        new_status: TaskStatus = "assigned" if downstream.assigned_to else "pending"
        updated = await db.update_task(
            downstream.id,
            status=new_status,
            note="dependencies satisfied, unblocked",
        )
        if updated is None:
            continue
        extra_targets: list[str] = []
        if updated.assigned_to is None:
            matched = await db.find_matching_agents(
                required_all=updated.required_all,
                required_any=updated.required_any,
                status="online",
            )
            extra_targets = [a.id for a in matched]
        await hub.emit_task_event("created", updated, extra_targets=extra_targets)


async def _propagate_failure(
    upstream: Task,
    upstream_action: str,
    db: Database,
    hub: WebSocketHub,
) -> None:
    """When `upstream` enters a terminal failure state (failed or deleted),
    cascade failure to all blocked downstream tasks. Recursive — the
    cascaded failures further unblock-fail their own downstream."""
    candidates = await db.find_blocked_downstream(upstream.id)
    for downstream in candidates:
        cascaded = await db.update_task(
            downstream.id,
            status="failed",
            note=f"upstream dependency {upstream.id} {upstream_action}",
        )
        if cascaded is None:
            continue
        await hub.emit_task_event("failed", cascaded)
        await _propagate_failure(cascaded, "failed (cascade)", db, hub)


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

    # --- depends_on validation ---
    initial_status: TaskStatus
    if body.depends_on:
        # Each referenced id must exist
        dep_tasks = []
        for dep_id in body.depends_on:
            dep = await db.get_task(dep_id)
            if dep is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"depends_on id '{dep_id}' not found",
                )
            if dep.status in ("failed", "deleted"):
                raise HTTPException(
                    status_code=400,
                    detail=f"depends_on id '{dep_id}' is already {dep.status} — "
                    "downstream task would never run",
                )
            dep_tasks.append(dep)
        # Compute initial status based on whether all deps are done
        all_done = all(d.status == "completed" for d in dep_tasks)
        if all_done:
            initial_status = "assigned" if body.assigned_to else "pending"
        else:
            initial_status = "blocked"
    else:
        initial_status = "assigned" if body.assigned_to else "pending"

    task = await db.create_task(
        title=body.title,
        description=body.description,
        created_by=me.id,
        assigned_to=body.assigned_to,
        priority=body.priority,
        required_all=body.required_all,
        required_any=body.required_any,
        depends_on=body.depends_on,
        initial_status=initial_status,
    )
    if task.assigned_to:
        await db.set_current_task(task.assigned_to, task.id)

    # Only broadcast task_event:created when the task is actually claimable.
    # Blocked tasks stay invisible to workers until their deps complete.
    if task.status != "blocked":
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


@router.delete("/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
    hub: Annotated[WebSocketHub, Depends(get_hub)],
) -> None:
    task = await db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if me.id != task.created_by and me.id != task.assigned_to:
        raise HTTPException(
            status_code=403, detail="only creator or assignee may delete"
        )
    await db.delete_task(task_id)
    if task.assigned_to:
        await db.set_current_task(task.assigned_to, None)
    await hub.emit_task_event("deleted", task)
    # Cascade: downstream tasks blocked on this deleted one fail with note.
    await _propagate_failure(task, "deleted", db, hub)


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

    # Dependency cascade: completion may unblock downstream tasks; failure
    # may cascade through them. Both run AFTER the primary event is emitted
    # so downstream observers see the original event first.
    if updated.status == "completed":
        await _propagate_completion(updated, db, hub)
    elif updated.status == "failed":
        await _propagate_failure(updated, "failed", db, hub)

    return updated
