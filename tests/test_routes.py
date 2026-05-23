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


async def test_update_agent_capabilities(two_agents):
    app, (key_a, agent_a), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.patch(
            "/api/v1/agents/me",
            headers=_auth(key_a),
            json={"capabilities": ["model:claude-opus-4-7", "tier:opus"]},
        )
        assert r.status_code == 200
        assert r.json()["capabilities"] == ["model:claude-opus-4-7", "tier:opus"]

        r = await c.get("/api/v1/agents/me", headers=_auth(key_a))
        assert "tier:opus" in r.json()["capabilities"]


async def test_create_task_stores_capability_requirements(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={
                "title": "needs opus",
                "required_all": ["tier:opus"],
                "required_any": ["vision", "audio"],
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["required_all"] == ["tier:opus"]
        assert body["required_any"] == ["vision", "audio"]


async def test_claim_blocked_when_agent_lacks_capabilities(two_agents):
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "opus only", "required_all": ["tier:opus"]},
        )
        task_id = r.json()["id"]

        # bob has no capabilities → 403.
        r = await c.post(f"/api/v1/tasks/{task_id}/claim", headers=_auth(key_b))
        assert r.status_code == 403
        assert "capabilities" in r.json()["detail"]


async def test_claim_succeeds_when_capabilities_match(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # Give bob the required capability.
        await c.patch(
            "/api/v1/agents/me",
            headers=_auth(key_b),
            json={"capabilities": ["tier:opus", "vision"]},
        )
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={
                "title": "opus + vision",
                "required_all": ["tier:opus"],
                "required_any": ["vision", "audio"],
            },
        )
        task_id = r.json()["id"]

        r = await c.post(f"/api/v1/tasks/{task_id}/claim", headers=_auth(key_b))
        assert r.status_code == 200
        assert r.json()["assigned_to"] == agent_b.id


async def test_delete_task_by_creator(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks", headers=_auth(key_a), json={"title": "drop me"}
        )
        task_id = r.json()["id"]

        r = await c.delete(f"/api/v1/tasks/{task_id}", headers=_auth(key_a))
        assert r.status_code == 204

        r = await c.get(f"/api/v1/tasks/{task_id}", headers=_auth(key_a))
        assert r.status_code == 404


async def test_delete_task_by_assignee_clears_current_task(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "claim then bail"},
        )
        task_id = r.json()["id"]

        await c.post(f"/api/v1/tasks/{task_id}/claim", headers=_auth(key_b))
        r = await c.get("/api/v1/agents/me", headers=_auth(key_b))
        assert r.json()["current_task"] == task_id

        r = await c.delete(f"/api/v1/tasks/{task_id}", headers=_auth(key_b))
        assert r.status_code == 204

        r = await c.get("/api/v1/agents/me", headers=_auth(key_b))
        assert r.json()["current_task"] is None


async def test_delete_task_rejects_non_owner(two_agents):
    app, (key_a, _), (key_b, _) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "alice owns this"},
        )
        task_id = r.json()["id"]

        r = await c.delete(f"/api/v1/tasks/{task_id}", headers=_auth(key_b))
        assert r.status_code == 403


async def test_delete_missing_task_returns_404(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.delete("/api/v1/tasks/nonexistent", headers=_auth(key_a))
        assert r.status_code == 404


async def test_stale_detection_when_heartbeat_old(two_agents):
    app, (key_a, _), (_, agent_b) = two_agents
    # Backdate bob's heartbeat by 5 minutes so the route flags him stale.
    db: Database = app.state.db
    from datetime import timedelta

    from mab.shared.models import utc_now

    old = utc_now() - timedelta(minutes=5)
    await db.conn.execute(
        "UPDATE agents SET last_heartbeat = ? WHERE id = ?",
        (old.isoformat(), agent_b.id),
    )
    await db.conn.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get(f"/api/v1/agents/{agent_b.id}", headers=_auth(key_a))
        body = r.json()
        assert body["is_stale"] is True
        assert body["last_heartbeat_age_seconds"] > 60


async def test_list_agents_enriched_with_freshness(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get("/api/v1/agents", headers=_auth(key_a))
        assert r.status_code == 200
        rows = r.json()
        for row in rows:
            assert "last_heartbeat_age_seconds" in row
            assert "is_stale" in row
            # Freshly seeded fixture; should be fresh.
            assert row["is_stale"] is False
            assert row["last_heartbeat_age_seconds"] < 60


async def test_match_agents_returns_only_matching(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # Bring bob online + give him a capability profile.
        await c.patch(
            "/api/v1/agents/me",
            headers=_auth(key_b),
            json={
                "status": "online",
                "capabilities": ["tier:opus", "vision"],
            },
        )

        r = await c.post(
            "/api/v1/agents/match",
            headers=_auth(key_a),
            json={"required_all": ["tier:opus"]},
        )
        assert r.status_code == 200
        names = [a["name"] for a in r.json()]
        assert names == ["bob"]


async def test_match_agents_available_only_excludes_busy(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        await c.patch(
            "/api/v1/agents/me",
            headers=_auth(key_b),
            json={"status": "online", "capabilities": ["tier:opus"]},
        )

        # Tie bob to a task.
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "occupy bob", "assigned_to": agent_b.id},
        )
        assert r.status_code == 201

        # Without available_only: bob matches.
        r = await c.post(
            "/api/v1/agents/match",
            headers=_auth(key_a),
            json={"required_all": ["tier:opus"], "available_only": False},
        )
        assert {a["name"] for a in r.json()} == {"bob"}

        # With available_only: bob is busy → excluded.
        r = await c.post(
            "/api/v1/agents/match",
            headers=_auth(key_a),
            json={"required_all": ["tier:opus"], "available_only": True},
        )
        assert r.json() == []


async def test_match_agents_empty_specs_returns_all_online(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # Seeded agents register as offline (no WS connect); pass status=null to
        # see them all — the route default is status="online".
        r = await c.post(
            "/api/v1/agents/match",
            headers=_auth(key_a),
            json={"status": None},
        )
        assert r.status_code == 200
        assert {a["name"] for a in r.json()} == {"alice", "bob"}


async def test_match_agents_filters_by_status(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # Fixture seeds agents with status="offline" (gen-key default since they
        # haven't connected via WS).
        r = await c.post(
            "/api/v1/agents/match",
            headers=_auth(key_a),
            json={"status": "online"},
        )
        assert r.json() == []


async def test_depends_on_blocks_until_upstream_completes(two_agents):
    app, (key_a, _agent_a), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "step 1", "assigned_to": agent_b.id},
        )
        upstream_id = r.json()["id"]
        assert r.json()["status"] == "assigned"

        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "step 2", "depends_on": [upstream_id]},
        )
        assert r.status_code == 201
        downstream_id = r.json()["id"]
        assert r.json()["status"] == "blocked"
        assert r.json()["depends_on"] == [upstream_id]

        # Blocked task can't be claimed
        r = await c.post(
            f"/api/v1/tasks/{downstream_id}/claim",
            headers=_auth(key_b),
        )
        assert r.status_code == 409
        assert "blocked" in r.json()["detail"]

        # Complete the upstream
        r = await c.patch(
            f"/api/v1/tasks/{upstream_id}",
            headers=_auth(key_b),
            json={"status": "completed", "result": "step 1 done"},
        )
        assert r.status_code == 200

        # Downstream should now be pending (unblocked)
        r = await c.get(
            f"/api/v1/tasks/{downstream_id}", headers=_auth(key_a)
        )
        assert r.json()["status"] == "pending"
        assert any(
            "unblocked" in note for note in r.json()["notes"]
        )


async def test_depends_on_fan_in_waits_for_all(two_agents):
    app, (key_a, _agent_a), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # Two upstream tasks both directly-assigned to bob
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "up-1", "assigned_to": agent_b.id},
        )
        up1 = r.json()["id"]
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "up-2", "assigned_to": agent_b.id},
        )
        up2 = r.json()["id"]

        # Downstream blocked on both
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={
                "title": "merge",
                "depends_on": [up1, up2],
                "assigned_to": agent_b.id,
            },
        )
        merge = r.json()["id"]
        assert r.json()["status"] == "blocked"

        # Complete only up-1 — downstream should remain blocked
        await c.patch(
            f"/api/v1/tasks/{up1}",
            headers=_auth(key_b),
            json={"status": "completed", "result": "up1 done"},
        )
        r = await c.get(f"/api/v1/tasks/{merge}", headers=_auth(key_a))
        assert r.json()["status"] == "blocked"

        # Complete up-2 too — now downstream unblocks
        await c.patch(
            f"/api/v1/tasks/{up2}",
            headers=_auth(key_b),
            json={"status": "completed", "result": "up2 done"},
        )
        r = await c.get(f"/api/v1/tasks/{merge}", headers=_auth(key_a))
        # `assigned` because downstream had assigned_to set at create time
        assert r.json()["status"] == "assigned"


async def test_depends_on_upstream_failure_cascades(two_agents):
    app, (key_a, _agent_a), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # 3-step chain: A -> B -> C
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "A", "assigned_to": agent_b.id},
        )
        a_id = r.json()["id"]
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "B", "depends_on": [a_id]},
        )
        b_id = r.json()["id"]
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "C", "depends_on": [b_id]},
        )
        c_id = r.json()["id"]

        # Both B and C should be blocked
        for tid in (b_id, c_id):
            r = await c.get(f"/api/v1/tasks/{tid}", headers=_auth(key_a))
            assert r.json()["status"] == "blocked"

        # Fail A → expect B + C to cascade to failed
        await c.patch(
            f"/api/v1/tasks/{a_id}",
            headers=_auth(key_b),
            json={"status": "failed", "note": "A broke"},
        )

        r = await c.get(f"/api/v1/tasks/{b_id}", headers=_auth(key_a))
        b_row = r.json()
        assert b_row["status"] == "failed"
        assert any("upstream" in note for note in b_row["notes"])

        r = await c.get(f"/api/v1/tasks/{c_id}", headers=_auth(key_a))
        c_row = r.json()
        assert c_row["status"] == "failed"
        assert any("cascade" in note for note in c_row["notes"])


async def test_depends_on_rejects_unknown_id(two_agents):
    app, (key_a, _), _ = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "orphan", "depends_on": ["nonexistent"]},
        )
        assert r.status_code == 400
        assert "not found" in r.json()["detail"]


async def test_depends_on_rejects_already_failed_dep(two_agents):
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # Create + fail an upstream
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "dead", "assigned_to": agent_b.id},
        )
        dead_id = r.json()["id"]
        await c.patch(
            f"/api/v1/tasks/{dead_id}",
            headers=_auth(key_b),
            json={"status": "failed"},
        )

        # Try to depend on it
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "downstream of dead", "depends_on": [dead_id]},
        )
        assert r.status_code == 400
        assert "failed" in r.json()["detail"]


async def test_depends_on_unblocks_when_dep_already_completed(two_agents):
    """If you create a task whose deps are ALREADY completed at create time,
    it should start pending (not blocked) and broadcast normally."""
    app, (key_a, _), (key_b, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "fast", "assigned_to": agent_b.id},
        )
        up = r.json()["id"]
        await c.patch(
            f"/api/v1/tasks/{up}",
            headers=_auth(key_b),
            json={"status": "completed", "result": "fast done"},
        )

        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={"title": "downstream", "depends_on": [up]},
        )
        assert r.status_code == 201
        assert r.json()["status"] == "pending"


async def test_directed_assignment_validates_capabilities(two_agents):
    app, (key_a, _), (_, agent_b) = two_agents
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        # bob has no capabilities; directed assignment with reqs must reject.
        r = await c.post(
            "/api/v1/tasks",
            headers=_auth(key_a),
            json={
                "title": "wrong fit",
                "assigned_to": agent_b.id,
                "required_all": ["tier:opus"],
            },
        )
        assert r.status_code == 400
        assert "capabilities" in r.json()["detail"]
