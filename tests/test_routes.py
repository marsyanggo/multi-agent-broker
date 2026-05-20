from __future__ import annotations

import asyncio
from pathlib import Path

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mab.broker.db import Database
from mab.broker.keygen import gen_key
from mab.broker.routes import agents, messages, tasks
from mab.broker.websocket import WebSocketHub


@pytest_asyncio.fixture
async def two_agents(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    await db.connect()

    app = FastAPI()
    app.state.db = db
    app.state.hub = WebSocketHub(db)
    app.include_router(agents.router)
    app.include_router(messages.router)
    app.include_router(tasks.router)

    key_a, agent_a = await gen_key(db, name="alice")
    key_b, agent_b = await gen_key(db, name="bob")
    try:
        yield app, (key_a, agent_a), (key_b, agent_b)
    finally:
        await db.close()


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_list_agents(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get("/api/v1/agents", headers=_auth(key_a))
        assert r.status_code == 200
        names = {a["name"] for a in r.json()}
        assert names == {"alice", "bob"}


async def test_get_me_and_by_id(two_agents):
    app, (key_a, agent_a), (_, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get("/api/v1/agents/me", headers=_auth(key_a))
        assert r.status_code == 200 and r.json()["id"] == agent_a.id

        r = await c.get(f"/api/v1/agents/{agent_b.id}", headers=_auth(key_a))
        assert r.status_code == 200 and r.json()["name"] == "bob"

        r = await c.get("/api/v1/agents/nonexistent", headers=_auth(key_a))
        assert r.status_code == 404


async def test_update_agent_status(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.patch(
            "/api/v1/agents/me", headers=_auth(key_a), json={"status": "busy"}
        )
        assert r.status_code == 200 and r.json()["status"] == "busy"


async def test_send_and_get_message(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/messages",
            headers=_auth(key_a),
            json={"to_agent": agent_b.id, "content": "hi bob"},
        )
        assert r.status_code == 201

        r = await c.get("/api/v1/messages", headers=_auth(key_b))
        assert r.status_code == 200
        msgs = r.json()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hi bob"
        assert msgs[0]["delivered"] is True


async def test_get_messages_does_not_remark_after_mark(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        await c.post(
            "/api/v1/messages",
            headers=_auth(key_a),
            json={"to_agent": agent_b.id, "content": "ping"},
        )
        await c.get("/api/v1/messages", headers=_auth(key_b))
        r = await c.get(
            "/api/v1/messages?only_undelivered=true", headers=_auth(key_b)
        )
        assert r.json() == []


async def test_send_to_unknown_agent_returns_404(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/messages",
            headers=_auth(key_a),
            json={"to_agent": "nonexistent", "content": "x"},
        )
        assert r.status_code == 404


async def test_task_lifecycle(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "fix bug", "priority": "high"},
        )
        assert r.status_code == 201
        task_id = r.json()["id"]
        assert r.json()["status"] == "pending"

        r = await c.post(f"/api/v1/tasks/{task_id}/claim", headers=_auth(key_b))
        assert r.status_code == 200
        assert r.json()["status"] == "assigned"
        assert r.json()["assigned_to"] == agent_b.id

        r = await c.post(f"/api/v1/tasks/{task_id}/claim", headers=_auth(key_a))
        assert r.status_code == 409

        r = await c.patch(
            f"/api/v1/tasks/{task_id}",
            headers=_auth(key_a),
            json={"status": "completed"},
        )
        assert r.status_code == 403

        r = await c.patch(
            f"/api/v1/tasks/{task_id}",
            headers=_auth(key_b),
            json={"status": "completed", "result": "fixed", "note": "took 2hrs"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert body["completed_at"] is not None
        assert body["notes"] == ["took 2hrs"]

        r = await c.get("/api/v1/agents/me", headers=_auth(key_b))
        assert r.json()["current_task"] is None


async def test_atomic_claim_via_api(two_agents):
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks", headers=_auth(key_a), json={"title": "race"}
        )
        task_id = r.json()["id"]

        r1, r2 = await asyncio.gather(
            c.post(f"/api/v1/tasks/{task_id}/claim", headers=_auth(key_a)),
            c.post(f"/api/v1/tasks/{task_id}/claim", headers=_auth(key_b)),
        )
        assert sorted([r1.status_code, r2.status_code]) == [200, 409]


async def test_list_tasks_filters(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        await c.post("/api/v1/tasks", headers=_auth(key_a), json={"title": "t1"})
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "t2", "assigned_to": agent_b.id},
        )
        assert r.json()["status"] == "assigned"

        r = await c.get(
            "/api/v1/tasks?status=pending", headers=_auth(key_a)
        )
        assert len(r.json()) == 1

        r = await c.get(
            f"/api/v1/tasks?assigned_to={agent_b.id}", headers=_auth(key_a)
        )
        assert len(r.json()) == 1
        assert r.json()[0]["title"] == "t2"
