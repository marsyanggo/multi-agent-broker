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


@router.post("/{task_id}/retry", response_model=Task)
async def retry_task(
    task_id: str,
    _me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
    hub: Annotated[WebSocketHub, Depends(get_hub)],
) -> Task:
    """Reset a failed task back to dispatchable (pending/blocked) so a worker
    can pick it up again. Same task id, lifecycle restarts in place — the
    dashboard sees red → gray → amber → green on a single node.

    Cascade: any downstream task that was failed with note
    "upstream dependency <task_id> failed" gets reset to blocked, so when
    this task succeeds the existing _propagate_completion path naturally
    unblocks the chain. Lead doesn't have to retry each step manually."""
    task = await db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != "failed":
        raise HTTPException(
            status_code=400,
            detail=f"only failed tasks can be retried (current status: {task.status})",
        )

    # Count prior retry attempts from notes.
    prior_attempts = sum(
        1 for n in (task.notes or []) if n.startswith("retry attempt ")
    )
    attempt_n = prior_attempts + 1

    # Was the failure a real error or a cascade?
    last_error = ""
    for n in reversed(task.notes or []):
        if n.startswith("adapter error:") or "timeout" in n or n.startswith("upstream"):
            last_error = n[:120]
            break

    # Compute reset status: blocked if any depend is not completed, else pending.
    new_status: str = "pending"
    for dep_id in task.depends_on or []:
        dep = await db.get_task(dep_id)
        if dep is None or dep.status != "completed":
            new_status = "blocked"
            break

    await db.reset_task_for_retry(task_id, new_status)

    note = f"retry attempt {attempt_n}"
    if last_error:
        note += f" (after: {last_error})"
    await db.update_task(task_id, note=note)

    # Clear the previous assignee's current_task pointer if it still
    # points at this task (worker would otherwise see ghost current_task).
    if task.assigned_to:
        prev_agent = await db.get_agent(task.assigned_to)
        if prev_agent and prev_agent.current_task == task_id:
            await db.set_current_task(task.assigned_to, None)

    refreshed = await db.get_task(task_id)
    assert refreshed is not None

    # Cascade reset: any downstream failed-by-this-upstream gets reset to
    # blocked. They'll auto-unblock via _propagate_completion when this
    # task succeeds.
    await _reset_cascade_failures(task_id, db)

    # Emit task_event:created so workers' wait_for_task pops it.
    # Only broadcast if it's now pending (workers don't pick blocked).
    if refreshed.status == "pending":
        extra_targets: list[str] = []
        if refreshed.assigned_to is None and (refreshed.required_all or refreshed.required_any):
            matched = await db.find_matching_agents(
                required_all=refreshed.required_all,
                required_any=refreshed.required_any,
                status="online",
            )
            extra_targets = [a.id for a in matched]
        await hub.emit_task_event(
            "created", refreshed, extra_targets=extra_targets
        )

    return refreshed


async def _reset_cascade_failures(upstream_id: str, db: Database) -> None:
    """Walk downstream: any task currently `failed` whose notes show it
    was cascade-failed from `upstream_id` (or any upstream that we just
    reset) goes back to `blocked`. Recursive — a long failure chain
    resets all the way down on a single retry call."""
    all_tasks = await db.list_tasks(status="failed", limit=10_000)
    expected_note = f"upstream dependency {upstream_id}"
    for t in all_tasks:
        if any(expected_note in n for n in (t.notes or [])):
            await db.reset_task_for_retry(t.id, "blocked")
            await db.update_task(
                t.id, note=f"reset to blocked (upstream {upstream_id} retrying)"
            )
            await _reset_cascade_failures(t.id, db)
