from __future__ import annotations

from pathlib import Path

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mab.broker.db import Database
from mab.broker.keygen import gen_key
from mab.broker.routes import agents, contexts, messages, tasks
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
    app.include_router(contexts.router)

    key_a, agent_a = await gen_key(db, name="alice")
    key_b, agent_b = await gen_key(db, name="bob")
    try:
        yield app, (key_a, agent_a), (key_b, agent_b)
    finally:
        await db.close()


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_create_context_basic(two_agents):
    app, (key_a, agent_a), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/contexts",
            headers=_auth(key_a),
            json={"name": "spec", "content": "# Project Spec\n\nBe terse."},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "spec"
        assert body["content"] == "# Project Spec\n\nBe terse."
        assert body["content_type"] == "text/markdown"
        assert body["created_by"] == agent_a.id
        assert body["task_id"] is None


async def test_create_context_with_task_link(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # First make a task to link to
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "upstream", "assigned_to": agent_b.id},
        )
        task_id = r.json()["id"]
        await c.patch(
            f"/api/v1/tasks/{task_id}",
            headers=_auth(key_b),
            json={"status": "completed", "result": "the answer is 42"},
        )

        # Auto-promote result to context
        r = await c.post(
            "/api/v1/contexts",
            headers=_auth(key_a),
            json={
                "name": "step-A-output",
                "content": "the answer is 42",
                "content_type": "text/plain",
                "task_id": task_id,
            },
        )
        assert r.status_code == 201
        assert r.json()["task_id"] == task_id


async def test_get_context_404(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get("/api/v1/contexts/nonexistent", headers=_auth(key_a))
        assert r.status_code == 404


async def test_list_contexts_filters_by_name(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        for content in ["draft 1", "draft 2", "draft 3"]:
            await c.post(
                "/api/v1/contexts",
                headers=_auth(key_a),
                json={"name": "draft", "content": content},
            )
        await c.post(
            "/api/v1/contexts",
            headers=_auth(key_a),
            json={"name": "other", "content": "elsewhere"},
        )

        r = await c.get("/api/v1/contexts?name=draft", headers=_auth(key_a))
        assert r.status_code == 200
        drafts = r.json()
        assert len(drafts) == 3
        assert all(d["name"] == "draft" for d in drafts)
        # ordered by updated_at DESC — newest first
        assert drafts[0]["content"] == "draft 3"


async def test_list_contexts_filters_by_task_id(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "t1", "assigned_to": agent_b.id},
        )
        t1 = r.json()["id"]
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "t2", "assigned_to": agent_b.id},
        )
        t2 = r.json()["id"]

        await c.post(
            "/api/v1/contexts",
            headers=_auth(key_a),
            json={"name": "t1-result", "content": "x", "task_id": t1},
        )
        await c.post(
            "/api/v1/contexts",
            headers=_auth(key_a),
            json={"name": "t2-result", "content": "y", "task_id": t2},
        )

        r = await c.get(
            f"/api/v1/contexts?task_id={t1}", headers=_auth(key_a)
        )
        ctxs = r.json()
        assert len(ctxs) == 1
        assert ctxs[0]["task_id"] == t1


async def test_update_context_creator_only(two_agents):
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/contexts",
            headers=_auth(key_a),
            json={"name": "alice's", "content": "original"},
        )
        cid = r.json()["id"]

        # Alice updates her own — OK
        r = await c.patch(
            f"/api/v1/contexts/{cid}",
            headers=_auth(key_a),
            json={"content": "updated"},
        )
        assert r.status_code == 200
        assert r.json()["content"] == "updated"

        # Bob tries — 403
        r = await c.patch(
            f"/api/v1/contexts/{cid}",
            headers=_auth(key_b),
            json={"content": "hacked"},
        )
        assert r.status_code == 403


async def test_update_context_partial(two_agents):
    """name and content can be updated independently."""
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/contexts",
            headers=_auth(key_a),
            json={"name": "old name", "content": "original"},
        )
        cid = r.json()["id"]

        # Update only name
        r = await c.patch(
            f"/api/v1/contexts/{cid}",
            headers=_auth(key_a),
            json={"name": "new name"},
        )
        assert r.json()["name"] == "new name"
        assert r.json()["content"] == "original"

        # Update only content
        r = await c.patch(
            f"/api/v1/contexts/{cid}",
            headers=_auth(key_a),
            json={"content": "new content"},
        )
        assert r.json()["name"] == "new name"
        assert r.json()["content"] == "new content"


async def test_delete_context_creator_only(two_agents):
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/contexts",
            headers=_auth(key_a),
            json={"name": "doomed", "content": "x"},
        )
        cid = r.json()["id"]

        # Bob can't delete
        r = await c.delete(
            f"/api/v1/contexts/{cid}", headers=_auth(key_b)
        )
        assert r.status_code == 403

        # Alice can
        r = await c.delete(
            f"/api/v1/contexts/{cid}", headers=_auth(key_a)
        )
        assert r.status_code == 204

        # Now gone
        r = await c.get(
            f"/api/v1/contexts/{cid}", headers=_auth(key_a)
        )
        assert r.status_code == 404


async def test_any_agent_can_read(two_agents):
    """Contexts are share docs — any authenticated agent can read."""
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/contexts",
            headers=_auth(key_a),
            json={"name": "alice's-pin", "content": "for everyone"},
        )
        cid = r.json()["id"]

        # Bob reads
        r = await c.get(
            f"/api/v1/contexts/{cid}", headers=_auth(key_b)
        )
        assert r.status_code == 200
        assert r.json()["content"] == "for everyone"
