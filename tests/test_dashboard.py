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


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_dashboard_snapshot_unauth(setup):
    app, _ = setup
    with TestClient(app) as client:
        r = client.get("/api/v1/dashboard/snapshot")
        assert r.status_code == 401


def test_dashboard_snapshot_basic_shape(setup):
    app, ((key_a, agent_a), _) = setup
    with TestClient(app) as client:
        r = client.get("/api/v1/dashboard/snapshot", headers=_bearer(key_a))
        assert r.status_code == 200
        data = r.json()

        # Top-level keys present.
        assert set(data.keys()) == {
            "agents",
            "tasks",
            "channels",
            "contexts",
            "server_time",
        }

        # Agents are AgentSnapshot shape — has freshness fields.
        assert len(data["agents"]) == 2  # alice + bob
        a0 = data["agents"][0]
        assert "id" in a0
        assert "last_heartbeat_age_seconds" in a0
        assert "is_stale" in a0

        # Tasks / channels / contexts start empty.
        assert data["tasks"] == []
        assert data["channels"] == []
        assert data["contexts"] == []


def test_dashboard_snapshot_includes_created_state(setup):
    app, ((key_a, agent_a), (key_b, agent_b)) = setup
    with TestClient(app) as client:
        # Create a task.
        r = client.post(
            "/api/v1/tasks",
            headers=_bearer(key_a),
            json={"title": "demo task", "description": "x"},
        )
        assert r.status_code == 201
        task_id = r.json()["id"]

        # Create a context.
        r = client.post(
            "/api/v1/contexts",
            headers=_bearer(key_a),
            json={"name": "spec", "content": "hello world"},
        )
        assert r.status_code == 201

        # Create a channel.
        r = client.post(
            "/api/v1/channels",
            headers=_bearer(key_a),
            json={"name": "general"},
        )
        assert r.status_code == 201

        # Bob joins, then alice posts a message.
        ch_id = r.json()["id"]
        r = client.post(
            f"/api/v1/channels/{ch_id}/join", headers=_bearer(key_b)
        )
        assert r.status_code == 200
        r = client.post(
            f"/api/v1/channels/{ch_id}/messages",
            headers=_bearer(key_a),
            json={"content": "hi all"},
        )
        assert r.status_code == 201

        # Snapshot now reflects everything.
        r = client.get("/api/v1/dashboard/snapshot", headers=_bearer(key_a))
        assert r.status_code == 200
        data = r.json()

        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == task_id

        assert len(data["channels"]) == 1
        ch_summary = data["channels"][0]
        assert ch_summary["channel"]["name"] == "general"
        assert ch_summary["member_count"] == 2  # alice (creator) + bob
        assert ch_summary["message_count"] == 1

        assert len(data["contexts"]) == 1
        assert data["contexts"][0]["name"] == "spec"
        assert data["contexts"][0]["content"] == "hello world"
