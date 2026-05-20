from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mab.broker.db import Database
from mab.broker.keygen import gen_key
from mab.broker.routes import agents, messages, tasks
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


def test_ws_rejects_missing_auth(setup):
    app, _ = setup
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/ws"):
                pass


def test_ws_rejects_bad_token(setup):
    app, _ = setup
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/v1/ws", headers={"Authorization": "Bearer bogus"}
            ):
                pass


def test_ws_self_ready_event_on_connect(setup):
    app, ((key_a, agent_a), _) = setup
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/ws", headers=_bearer(key_a)
        ) as ws:
            data = ws.receive_json()
            assert data["type"] == "agent_event"
            assert data["payload"]["event"] == "online"
            assert data["payload"]["agent"]["id"] == agent_a.id
            assert data["payload"]["agent"]["status"] == "online"


def test_ws_live_message_delivery(setup):
    app, ((key_a, _), (key_b, agent_b)) = setup
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/ws", headers=_bearer(key_b)
        ) as ws_b:
            ws_b.receive_json()  # consume self-ready

            r = client.post(
                "/api/v1/messages",
                headers=_bearer(key_a),
                json={"to_agent": agent_b.id, "content": "hi"},
            )
            assert r.status_code == 201
            assert r.json()["delivered"] is True

            data = ws_b.receive_json()
            assert data["type"] == "message"
            assert data["payload"]["message"]["content"] == "hi"


def test_ws_offline_backfill_on_reconnect(setup):
    app, ((key_a, _), (key_b, agent_b)) = setup
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/messages",
            headers=_bearer(key_a),
            json={"to_agent": agent_b.id, "content": "stored"},
        )
        assert r.status_code == 201
        assert r.json()["delivered"] is False

        with client.websocket_connect(
            "/api/v1/ws", headers=_bearer(key_b)
        ) as ws_b:
            ws_b.receive_json()  # consume self-ready
            data = ws_b.receive_json()
            assert data["type"] == "message"
            assert data["payload"]["message"]["content"] == "stored"


def test_ws_agent_event_online_broadcast(setup):
    app, ((key_a, _), (key_b, agent_b)) = setup
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/ws", headers=_bearer(key_a)
        ) as ws_a:
            ws_a.receive_json()  # self-ready
            with client.websocket_connect(
                "/api/v1/ws", headers=_bearer(key_b)
            ) as ws_b:
                ws_b.receive_json()  # self-ready
                online = ws_a.receive_json()
                assert online["type"] == "agent_event"
                assert online["payload"]["event"] == "online"
                assert online["payload"]["agent"]["id"] == agent_b.id


def test_ws_task_event_created(setup):
    app, ((key_a, _), (key_b, agent_b)) = setup
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/ws", headers=_bearer(key_b)
        ) as ws_b:
            ws_b.receive_json()  # self-ready

            r = client.post(
                "/api/v1/tasks",
                headers=_bearer(key_a),
                json={"title": "do thing", "assigned_to": agent_b.id},
            )
            assert r.status_code == 201

            data = ws_b.receive_json()
            assert data["type"] == "task_event"
            assert data["payload"]["event"] == "created"
            assert data["payload"]["task"]["title"] == "do thing"


def test_ws_task_event_claim_notifies_creator(setup):
    app, ((key_a, agent_a), (key_b, _)) = setup
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/ws", headers=_bearer(key_a)
        ) as ws_a:
            ws_a.receive_json()  # self-ready

            r = client.post(
                "/api/v1/tasks", headers=_bearer(key_a), json={"title": "race"}
            )
            task_id = r.json()["id"]

            # Alice receives task_event(created) since she's the creator
            created_ev = ws_a.receive_json()
            assert created_ev["type"] == "task_event"
            assert created_ev["payload"]["event"] == "created"

            # Bob claims via REST
            r = client.post(
                f"/api/v1/tasks/{task_id}/claim", headers=_bearer(key_b)
            )
            assert r.status_code == 200

            claimed_ev = ws_a.receive_json()
            assert claimed_ev["type"] == "task_event"
            assert claimed_ev["payload"]["event"] == "claimed"
            assert claimed_ev["payload"]["task"]["assigned_to"] is not None
