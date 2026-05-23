from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from mab.broker.db import Database
from mab.broker.keygen import gen_key
from mab.broker.routes import agents, channels, contexts, messages, tasks
from mab.broker.websocket import WebSocketHub, router as ws_router


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
    app.include_router(channels.router)

    key_a, agent_a = await gen_key(db, name="alice")
    key_b, agent_b = await gen_key(db, name="bob")
    try:
        yield app, (key_a, agent_a), (key_b, agent_b)
    finally:
        await db.close()


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_create_channel_basic(two_agents):
    app, (key_a, agent_a), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/channels",
            headers=_auth(key_a),
            json={"name": "general", "description": "all hands"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "general"
        assert body["description"] == "all hands"
        assert body["created_by"] == agent_a.id


async def test_create_channel_auto_joins_creator(two_agents):
    app, (key_a, agent_a), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/channels", headers=_auth(key_a), json={"name": "dev"}
        )
        cid = r.json()["id"]
        r = await c.get(f"/api/v1/channels/{cid}", headers=_auth(key_a))
        body = r.json()
        assert agent_a.id in body["members"]


async def test_duplicate_name_conflicts(two_agents):
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        await c.post(
            "/api/v1/channels", headers=_auth(key_a), json={"name": "ops"}
        )
        r = await c.post(
            "/api/v1/channels", headers=_auth(key_b), json={"name": "ops"}
        )
        assert r.status_code == 409


async def test_auto_suffix_resolves_conflict(two_agents):
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        await c.post(
            "/api/v1/channels", headers=_auth(key_a), json={"name": "alpha"}
        )
        r = await c.post(
            "/api/v1/channels",
            headers=_auth(key_b),
            json={"name": "alpha", "auto_suffix": True},
        )
        assert r.status_code == 201
        assert r.json()["name"] == "alpha-2"


async def test_join_and_leave_channel(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/channels", headers=_auth(key_a), json={"name": "team"}
        )
        cid = r.json()["id"]

        # Bob joins
        r = await c.post(f"/api/v1/channels/{cid}/join", headers=_auth(key_b))
        assert r.status_code == 200
        assert agent_b.id in r.json()["members"]

        # Bob leaves
        r = await c.post(f"/api/v1/channels/{cid}/leave", headers=_auth(key_b))
        assert r.status_code == 204
        r = await c.get(f"/api/v1/channels/{cid}", headers=_auth(key_a))
        assert agent_b.id not in r.json()["members"]


async def test_list_channels_my_membership(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # Alice creates two channels; Bob only joins one
        r = await c.post(
            "/api/v1/channels", headers=_auth(key_a), json={"name": "x"}
        )
        x_id = r.json()["id"]
        await c.post(
            "/api/v1/channels", headers=_auth(key_a), json={"name": "y"}
        )
        await c.post(f"/api/v1/channels/{x_id}/join", headers=_auth(key_b))

        # Bob: all → 2 channels
        r = await c.get("/api/v1/channels", headers=_auth(key_b))
        assert len(r.json()) == 2

        # Bob: my_membership → 1 (just x)
        r = await c.get(
            "/api/v1/channels?my_membership=true", headers=_auth(key_b)
        )
        names = [c["name"] for c in r.json()]
        assert names == ["x"]


async def test_post_message_requires_membership(two_agents):
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/channels", headers=_auth(key_a), json={"name": "private"}
        )
        cid = r.json()["id"]

        # Bob (not a member) tries to post → 403
        r = await c.post(
            f"/api/v1/channels/{cid}/messages",
            headers=_auth(key_b),
            json={"content": "intrude"},
        )
        assert r.status_code == 403
        assert "join" in r.json()["detail"]


async def test_post_and_list_messages(two_agents):
    app, (key_a, agent_a), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/channels", headers=_auth(key_a), json={"name": "log"}
        )
        cid = r.json()["id"]

        for content in ["m1", "m2", "m3"]:
            r = await c.post(
                f"/api/v1/channels/{cid}/messages",
                headers=_auth(key_a),
                json={"content": content},
            )
            assert r.status_code == 201
            assert r.json()["content"] == content
            assert r.json()["from_agent"] == agent_a.id

        r = await c.get(
            f"/api/v1/channels/{cid}/messages", headers=_auth(key_a)
        )
        assert r.status_code == 200
        msgs = r.json()
        assert [m["content"] for m in msgs] == ["m1", "m2", "m3"]


async def test_delete_channel_creator_only(two_agents):
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/channels", headers=_auth(key_a), json={"name": "doomed"}
        )
        cid = r.json()["id"]

        # Bob can't delete
        r = await c.delete(f"/api/v1/channels/{cid}", headers=_auth(key_b))
        assert r.status_code == 403

        # Alice can
        r = await c.delete(f"/api/v1/channels/{cid}", headers=_auth(key_a))
        assert r.status_code == 204
        r = await c.get(f"/api/v1/channels/{cid}", headers=_auth(key_a))
        assert r.status_code == 404


# --- WS broadcast tests (real uvicorn-mounted FastAPI) ---


@pytest.fixture
def ws_setup(tmp_path: Path):
    """Like the test_websocket.py setup — TestClient with WS + lifespan."""
    db_path = tmp_path / "ws.db"

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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        d = Database(db_path)
        await d.connect()
        app.state.db = d
        app.state.hub = WebSocketHub(d)
        try:
            yield
        finally:
            await d.close()

    fa = FastAPI(lifespan=lifespan)
    fa.include_router(agents.router)
    fa.include_router(messages.router)
    fa.include_router(tasks.router)
    fa.include_router(contexts.router)
    fa.include_router(channels.router)
    fa.include_router(ws_router)
    return fa, creds


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_ws_channel_message_broadcasts_to_members(ws_setup):
    """When a member posts to a channel, all other online members receive
    the message via WS push."""
    app, ((key_a, _agent_a), (key_b, _agent_b)) = ws_setup
    with TestClient(app) as client:
        # Alice creates channel; Bob joins it
        r = client.post(
            "/api/v1/channels", headers=_bearer(key_a), json={"name": "team-sync"}
        )
        cid = r.json()["id"]
        client.post(f"/api/v1/channels/{cid}/join", headers=_bearer(key_b))

        # Bob opens WS
        with client.websocket_connect("/api/v1/ws", headers=_bearer(key_b)) as ws_b:
            ws_b.receive_json()  # consume self-ready

            # Alice posts a message
            r = client.post(
                f"/api/v1/channels/{cid}/messages",
                headers=_bearer(key_a),
                json={"content": "all hands at 3pm"},
            )
            assert r.status_code == 201

            data = ws_b.receive_json()
            assert data["type"] == "channel_message"
            assert data["payload"]["message"]["content"] == "all hands at 3pm"
            assert data["payload"]["message"]["channel_id"] == cid


def test_ws_channel_message_not_delivered_to_non_members(ws_setup):
    """A non-member's WS queue should NOT receive channel messages."""
    app, ((key_a, _agent_a), (key_b, agent_b)) = ws_setup
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/channels", headers=_bearer(key_a), json={"name": "alice-only"}
        )
        cid = r.json()["id"]
        # Bob does NOT join

        with client.websocket_connect("/api/v1/ws", headers=_bearer(key_b)) as ws_b:
            ws_b.receive_json()  # self-ready

            # Alice posts; Bob is not a member
            client.post(
                f"/api/v1/channels/{cid}/messages",
                headers=_bearer(key_a),
                json={"content": "should not reach bob"},
            )

            # Anchor with a direct message so we can detect "no channel_message arrived"
            client.post(
                "/api/v1/messages",
                headers=_bearer(key_a),
                json={"to_agent": agent_b.id, "content": "ping"},
            )

            data = ws_b.receive_json()
            # Bob's first received envelope should be the direct message anchor,
            # not the channel_message — proving the broker filtered.
            assert data["type"] == "message"
            assert data["payload"]["message"]["content"] == "ping"
