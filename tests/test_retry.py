"""Retry endpoint + cascade reset tests."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mab.broker.db import Database
from mab.broker.keygen import gen_key
from mab.broker.routes import agents, channels, contexts, dashboard, messages, tasks
from mab.broker.websocket import WebSocketHub, router as ws_router


def _build_app(db_path: Path) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(db_path)
        await db.connect()
        app.state.db = db
        app.state.hub = WebSocketHub(db)
        try:
            yield
        finally:
            await db.close()

    fa = FastAPI(lifespan=lifespan)
    fa.include_router(agents.router)
    fa.include_router(messages.router)
    fa.include_router(tasks.router)
    fa.include_router(contexts.router)
    fa.include_router(channels.router)
    fa.include_router(dashboard.router)
    fa.include_router(ws_router)
    return fa


@pytest.fixture
def setup(tmp_path: Path):
    db_path = tmp_path / "test.db"

    async def _seed():
        d = Database(db_path)
        await d.connect()
        try:
            ka, aa = await gen_key(d, name="alice")
            kb, ab = await gen_key(d, name="bob")
            return (ka, aa), (kb, ab)
        finally:
            await d.close()

    creds = asyncio.run(_seed())
    return _build_app(db_path), creds


def _b(k: str) -> dict:
    return {"Authorization": f"Bearer {k}"}


def _create_and_fail(client: TestClient, key: str, agent_id: str, title: str = "t") -> str:
    """Create a task assigned to agent_id and mark it failed by that agent."""
    r = client.post(
        "/api/v1/tasks",
        headers=_b(key),
        json={"title": title, "description": "x", "assigned_to": agent_id},
    )
    tid = r.json()["id"]
    r = client.patch(
        f"/api/v1/tasks/{tid}",
        headers=_b(key),
        json={"status": "failed", "note": "adapter error: connection refused"},
    )
    assert r.json()["status"] == "failed"
    return tid


def test_retry_resets_failed_to_pending(setup):
    app, ((kb, ab), _) = setup
    with TestClient(app) as c:
        tid = _create_and_fail(c, kb, ab.id)
        r = c.post(f"/api/v1/tasks/{tid}/retry", headers=_b(kb))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["result"] is None
        assert body["completed_at"] is None
        assert body["assigned_to"] is None
        notes = body["notes"]
        assert any(n.startswith("retry attempt 1") for n in notes)


def test_retry_rejects_non_failed(setup):
    app, ((kb, ab), _) = setup
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/tasks",
            headers=_b(kb),
            json={"title": "x"},
        )
        tid = r.json()["id"]  # status=pending
        r = c.post(f"/api/v1/tasks/{tid}/retry", headers=_b(kb))
        assert r.status_code == 400
        assert "only failed" in r.text


def test_retry_404_on_missing(setup):
    app, ((kb, _),  _) = setup
    with TestClient(app) as c:
        r = c.post("/api/v1/tasks/no-such-id/retry", headers=_b(kb))
        assert r.status_code == 404


def test_retry_increments_attempt_counter(setup):
    app, ((kb, ab), _) = setup
    with TestClient(app) as c:
        tid = _create_and_fail(c, kb, ab.id)

        c.post(f"/api/v1/tasks/{tid}/retry", headers=_b(kb))  # attempt 1
        # Fail again — claim (POST), then PATCH to failed
        c.post(f"/api/v1/tasks/{tid}/claim", headers=_b(kb))
        c.patch(
            f"/api/v1/tasks/{tid}",
            headers=_b(kb),
            json={"status": "failed", "note": "second crash"},
        )
        r = c.post(f"/api/v1/tasks/{tid}/retry", headers=_b(kb))  # attempt 2
        notes = r.json()["notes"]
        assert any(n.startswith("retry attempt 1") for n in notes)
        assert any(n.startswith("retry attempt 2") for n in notes)


def test_retry_with_unmet_deps_returns_blocked(setup):
    """Retry a task whose upstream isn't completed yet — should land in
    `blocked`, not `pending`, since the dep gate isn't satisfied."""
    app, ((kb, ab), _) = setup
    with TestClient(app) as c:
        # upstream assigned to bob, stays in 'assigned' (not completed)
        r = c.post(
            "/api/v1/tasks",
            headers=_b(kb),
            json={"title": "up", "assigned_to": ab.id},
        )
        up_id = r.json()["id"]
        # downstream depending on up — starts blocked
        r = c.post(
            "/api/v1/tasks",
            headers=_b(kb),
            json={"title": "down", "depends_on": [up_id]},
        )
        down_id = r.json()["id"]
        assert r.json()["status"] == "blocked"

        # Fail up → cascade fails down
        r = c.patch(
            f"/api/v1/tasks/{up_id}",
            headers=_b(kb),
            json={"status": "failed", "note": "upstream boom"},
        )
        assert r.json()["status"] == "failed"
        down = c.get(f"/api/v1/tasks/{down_id}", headers=_b(kb)).json()
        assert down["status"] == "failed"

        # Retry down — up is still failed (not completed) so down should be blocked
        r = c.post(f"/api/v1/tasks/{down_id}/retry", headers=_b(kb))
        assert r.status_code == 200
        assert r.json()["status"] == "blocked"


def test_retry_cascade_resets_failed_downstream(setup):
    """If A failed and B was cascade-failed because of A, retrying A should
    reset B to blocked too (so when A succeeds, B unblocks automatically)."""
    app, ((kb, ab), _) = setup
    with TestClient(app) as c:
        # A
        r = c.post(
            "/api/v1/tasks",
            headers=_b(kb),
            json={"title": "A", "assigned_to": ab.id},
        )
        a_id = r.json()["id"]
        # B depends on A
        r = c.post(
            "/api/v1/tasks",
            headers=_b(kb),
            json={"title": "B", "depends_on": [a_id]},
        )
        b_id = r.json()["id"]
        assert r.json()["status"] == "blocked"

        # Fail A — B should cascade to failed
        r = c.patch(
            f"/api/v1/tasks/{a_id}",
            headers=_b(kb),
            json={"status": "failed", "note": "boom"},
        )
        assert r.json()["status"] == "failed"
        b = c.get(f"/api/v1/tasks/{b_id}", headers=_b(kb)).json()
        assert b["status"] == "failed"
        assert any(f"upstream dependency {a_id}" in n for n in b["notes"])

        # Retry A — both A and B should be reset
        r = c.post(f"/api/v1/tasks/{a_id}/retry", headers=_b(kb))
        assert r.status_code == 200
        assert r.json()["status"] == "pending"

        b = c.get(f"/api/v1/tasks/{b_id}", headers=_b(kb)).json()
        assert b["status"] == "blocked"
        assert any(f"reset to blocked" in n for n in b["notes"])
