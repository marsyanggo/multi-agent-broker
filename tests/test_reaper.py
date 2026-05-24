"""Stuck-task reaper unit tests.

Covers the broker-side rule: a task in `assigned` or `in_progress`
whose assignee hasn't heartbeat for longer than the stale threshold
should be auto-marked `failed`, the agent's `current_task` cleared,
and downstream blocked tasks cascaded.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from mab.broker.config import settings
from mab.broker.db import Database
from mab.broker.keygen import gen_key
from mab.broker.reaper import reap_stuck_tasks
from mab.broker.websocket import WebSocketHub
from mab.shared.models import utc_now


def _iso(dt):
    return dt.astimezone(dt.tzinfo or None).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


async def _make_db(tmp: Path) -> Database:
    db = Database(tmp / "test.db")
    await db.connect()
    return db


async def _force_stale_heartbeat(db: Database, agent_id: str, age_s: float) -> None:
    """Backdate an agent's heartbeat so the reaper considers it stale."""
    stale = utc_now() - timedelta(seconds=age_s)
    await db.conn.execute(
        "UPDATE agents SET last_heartbeat = ? WHERE id = ?",
        (stale.isoformat(), agent_id),
    )
    await db.conn.commit()


@pytest.mark.asyncio
async def test_reaper_marks_stuck_task_failed(tmp_path: Path):
    db = await _make_db(tmp_path)
    hub = WebSocketHub(db)
    try:
        _, alice = await gen_key(db, name="alice")
        _, bob = await gen_key(db, name="bob")

        task = await db.create_task(
            title="stuck", description="", created_by=alice.id, assigned_to=bob.id
        )
        await db.update_task(task.id, status="in_progress", note="working")
        await db.set_current_task(bob.id, task.id)

        # Bob's heartbeat is fresh from gen_key — reaper shouldn't touch.
        n = await reap_stuck_tasks(db, hub)
        assert n == 0

        # Backdate bob's heartbeat past the stale threshold.
        threshold = (
            settings.heartbeat_interval_seconds * settings.task_reap_stale_multiplier
        )
        await _force_stale_heartbeat(db, bob.id, threshold + 30)

        n = await reap_stuck_tasks(db, hub)
        assert n == 1

        reaped = await db.get_task(task.id)
        assert reaped is not None
        assert reaped.status == "failed"
        assert any("worker timeout" in note for note in reaped.notes)

        # Bob's current_task should be cleared so a respawn can pick up new work.
        bob_after = await db.get_agent(bob.id)
        assert bob_after.current_task is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reaper_skips_fresh_agent(tmp_path: Path):
    db = await _make_db(tmp_path)
    hub = WebSocketHub(db)
    try:
        _, alice = await gen_key(db, name="alice")
        _, bob = await gen_key(db, name="bob")

        task = await db.create_task(
            title="slow but alive",
            description="",
            created_by=alice.id,
            assigned_to=bob.id,
        )
        await db.update_task(task.id, status="in_progress")

        # Bob's heartbeat is fresh (default state from gen_key).
        n = await reap_stuck_tasks(db, hub)
        assert n == 0

        task_after = await db.get_task(task.id)
        assert task_after.status == "in_progress"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reaper_cascades_failure_downstream(tmp_path: Path):
    db = await _make_db(tmp_path)
    hub = WebSocketHub(db)
    try:
        _, alice = await gen_key(db, name="alice")
        _, bob = await gen_key(db, name="bob")

        upstream = await db.create_task(
            title="stuck upstream",
            description="",
            created_by=alice.id,
            assigned_to=bob.id,
        )
        await db.update_task(upstream.id, status="in_progress")
        await db.set_current_task(bob.id, upstream.id)

        downstream = await db.create_task(
            title="downstream",
            description="",
            created_by=alice.id,
            depends_on=[upstream.id],
            initial_status="blocked",
        )
        downstream = await db.get_task(downstream.id)
        assert downstream.status == "blocked"

        # Make bob stale, reap.
        threshold = (
            settings.heartbeat_interval_seconds * settings.task_reap_stale_multiplier
        )
        await _force_stale_heartbeat(db, bob.id, threshold + 30)

        n = await reap_stuck_tasks(db, hub)
        assert n == 1

        # Both upstream and downstream should be failed now.
        u_after = await db.get_task(upstream.id)
        d_after = await db.get_task(downstream.id)
        assert u_after.status == "failed"
        assert d_after.status == "failed"
        assert any("worker timeout" in n for n in u_after.notes)
        assert any(f"upstream dependency {upstream.id}" in n for n in d_after.notes)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reaper_ignores_pending_and_completed(tmp_path: Path):
    db = await _make_db(tmp_path)
    hub = WebSocketHub(db)
    try:
        _, alice = await gen_key(db, name="alice")
        _, bob = await gen_key(db, name="bob")

        # Pending — not yet claimed, no assignee.
        pending = await db.create_task(
            title="just dispatched", description="", created_by=alice.id
        )
        assert pending.status == "pending"
        assert pending.assigned_to is None

        # Completed — already done, even if assignee goes stale shouldn't matter.
        completed = await db.create_task(
            title="done", description="", created_by=alice.id, assigned_to=bob.id
        )
        await db.update_task(completed.id, status="completed", result="ok")

        # Backdate bob's heartbeat past stale.
        threshold = (
            settings.heartbeat_interval_seconds * settings.task_reap_stale_multiplier
        )
        await _force_stale_heartbeat(db, bob.id, threshold + 30)

        n = await reap_stuck_tasks(db, hub)
        assert n == 0  # nothing in assigned/in_progress to reap

        p_after = await db.get_task(pending.id)
        c_after = await db.get_task(completed.id)
        assert p_after.status == "pending"
        assert c_after.status == "completed"
    finally:
        await db.close()
