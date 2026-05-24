"""Stuck-task reaper.

Periodically scans tasks in `assigned` or `in_progress` whose owner agent
hasn't heartbeat for longer than the stale threshold. Marks them `failed`
with a note explaining why, clears the owner's `current_task`, emits a
task_event, and cascades failure downstream via depends_on (so the whole
chain doesn't hang waiting on a dead worker).

Triggered by the worker-dies-mid-task pattern: worker daemon crashes or
loses the network after writing status=in_progress but before writing
status=completed. Without this reaper such tasks stay in_progress
forever, polluting the dashboard "active" filter and blocking any
downstream depends_on chain.
"""

from __future__ import annotations

import asyncio
import logging

from mab.broker.config import settings
from mab.broker.db import Database
from mab.broker.routes.tasks import _propagate_failure
from mab.broker.websocket import WebSocketHub
from mab.shared.models import utc_now

log = logging.getLogger("mab.reaper")


async def reap_stuck_tasks(db: Database, hub: WebSocketHub) -> int:
    """One reap pass. Returns the number of tasks marked failed."""
    threshold = settings.heartbeat_interval_seconds * settings.task_reap_stale_multiplier
    now = utc_now()
    reaped = 0

    for status in ("assigned", "in_progress"):
        tasks = await db.list_tasks(status=status, limit=10_000)
        for t in tasks:
            if not t.assigned_to:
                continue
            agent = await db.get_agent(t.assigned_to)
            if agent is None:
                continue
            heartbeat_age = (now - agent.last_heartbeat).total_seconds()
            if heartbeat_age <= threshold:
                continue  # agent still healthy — task may legitimately be slow

            note = (
                f"worker timeout: agent {agent.name} ({agent.id}) heartbeat "
                f"silent {heartbeat_age:.0f}s (threshold {threshold}s)"
            )
            updated = await db.update_task(t.id, status="failed", note=note)
            if updated is None:
                continue

            # Clear the dead worker's current_task pointer so it can be
            # reused if the worker eventually reconnects.
            await db.set_current_task(agent.id, None)

            await hub.emit_task_event("failed", updated)
            await _propagate_failure(updated, "failed (worker timeout)", db, hub)
            reaped += 1
            log.warning(
                "reaped stuck task %s (%s) — %s", t.id, t.title[:60], note
            )

    return reaped


async def reaper_loop(db: Database, hub: WebSocketHub) -> None:
    """Periodic background task. Run from app lifespan."""
    while True:
        await asyncio.sleep(settings.task_reap_interval_seconds)
        try:
            n = await reap_stuck_tasks(db, hub)
            if n:
                log.info("reaper pass: %d task(s) marked failed", n)
        except Exception:
            log.exception("reaper pass failed")
