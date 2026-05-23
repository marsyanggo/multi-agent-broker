from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import mab.mcp_server.server as srv
from mab.mcp_server.broker_client import BrokerClient


EXPECTED_TOOLS = {
    "list_agents",
    "match_agents",
    "get_agent_info",
    "report_status",
    "update_my_model",
    "update_my_capabilities",
    "send_message",
    "get_messages",
    "create_task",
    "claim_task",
    "update_task",
    "delete_task",
    "list_tasks",
    "wait_for_task",
}


async def test_all_tools_registered():
    tools = await srv.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS


async def test_tools_have_descriptions():
    tools = await srv.mcp.list_tools()
    for t in tools:
        assert t.description, f"tool {t.name} missing description"


def _unwrap(out: str) -> tuple[Any, dict]:
    body = json.loads(out)
    return body["result"], {
        k: v for k, v in body.items() if k.startswith("_pending_")
    }


async def test_send_message_tool_against_live_broker(live_broker):
    url, (key_a, _), (_, agent_b) = live_broker
    srv._client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await srv._client.start()
        result, meta = _unwrap(
            await srv.send_message(to_agent=agent_b.id, content="hello via tool")
        )
        assert result["content"] == "hello via tool"
        assert result["to_agent"] == agent_b.id
        assert meta == {"_pending_messages": 0, "_pending_task_events": 0}
    finally:
        await srv._client.stop()
        srv._client = None


async def test_list_agents_tool_against_live_broker(live_broker):
    url, (key_a, _), _ = live_broker
    srv._client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await srv._client.start()
        result, _ = _unwrap(await srv.list_agents())
        names = {a["name"] for a in result}
        assert names == {"alice", "bob"}
    finally:
        await srv._client.stop()
        srv._client = None


async def test_task_tools_against_live_broker(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker
    srv._client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await srv._client.start()
        created, _ = _unwrap(await srv.create_task(title="mcp task", priority="high"))
        assert created["status"] == "pending"

        listed, _ = _unwrap(await srv.list_tasks(status="pending"))
        assert any(t["id"] == created["id"] for t in listed)
    finally:
        await srv._client.stop()
        srv._client = None


async def test_tool_without_client_raises():
    srv._client = None
    with pytest.raises(RuntimeError, match="not initialized"):
        await srv.list_agents()


async def test_report_status_validates_input():
    srv._client = None  # status validation runs before client check
    result, _ = _unwrap(await srv.report_status("bogus"))
    assert result["error"] == "invalid status"


async def test_update_my_model_derives_tags(live_broker):
    url, (key_a, _), _ = live_broker
    srv._client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await srv._client.start()
        result, _ = _unwrap(await srv.update_my_model("claude-opus-4-7"))
        caps = set(result["capabilities"])
        assert {"model:claude-opus-4-7", "family:claude", "tier:opus",
                "provider:anthropic"} <= caps
    finally:
        await srv._client.stop()
        srv._client = None


async def test_update_my_model_with_extras(live_broker):
    url, (key_a, _), _ = live_broker
    srv._client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await srv._client.start()
        result, _ = _unwrap(
            await srv.update_my_model(
                "claude-opus-4-7", extra_capabilities=["vision", "code-review"]
            )
        )
        caps = set(result["capabilities"])
        assert "vision" in caps and "code-review" in caps
        assert "tier:opus" in caps
    finally:
        await srv._client.stop()
        srv._client = None


async def test_wait_for_task_returns_pushed_task(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker
    client_a = BrokerClient(broker_url=url, api_key=key_a)
    srv._client = BrokerClient(broker_url=url, api_key=key_b)
    try:
        await client_a.start()
        await srv._client.start()

        # Alice creates a task assigned to bob — bob's queue should get it
        # via WS push.
        await client_a.create_task(title="bob's task", assigned_to=agent_b.id)

        # wait_for_task should pick it up within a couple seconds.
        result, _ = _unwrap(await srv.wait_for_task(timeout_seconds=3))
        assert isinstance(result, dict)
        assert result.get("title") == "bob's task"
        assert result.get("assigned_to") == agent_b.id
    finally:
        await client_a.stop()
        await srv._client.stop()
        srv._client = None


async def test_wait_for_task_returns_timeout_when_quiet(live_broker):
    url, (key_a, _), _ = live_broker
    srv._client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await srv._client.start()
        result, _ = _unwrap(await srv.wait_for_task(timeout_seconds=1))
        assert result == {"timeout": True}
    finally:
        await srv._client.stop()
        srv._client = None


async def test_wait_for_task_drops_non_actionable_echoes(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker
    client_a = BrokerClient(broker_url=url, api_key=key_a)
    srv._client = BrokerClient(broker_url=url, api_key=key_b)
    try:
        await client_a.start()
        await srv._client.start()

        # Alice creates a task assigned to herself — bob shouldn't see it as
        # actionable (status="assigned" but not to bob).
        await client_a.create_task(title="alice's task", assigned_to=client_a.agent.id)

        # Give push time to arrive then time out.
        result, _ = _unwrap(await srv.wait_for_task(timeout_seconds=1))
        assert result == {"timeout": True}
    finally:
        await client_a.stop()
        await srv._client.stop()
        srv._client = None


async def test_update_my_capabilities_replaces(live_broker):
    url, (key_a, _), _ = live_broker
    srv._client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await srv._client.start()
        await srv.update_my_model("claude-opus-4-7")
        result, _ = _unwrap(
            await srv.update_my_capabilities(["custom-tag-1", "custom-tag-2"])
        )
        # Full replacement — no opus tags left.
        assert set(result["capabilities"]) == {"custom-tag-1", "custom-tag-2"}
    finally:
        await srv._client.stop()
        srv._client = None


# --- T9: pending-count interceptor ---


async def test_response_always_wraps_with_pending_counts(live_broker):
    url, (key_a, _), _ = live_broker
    srv._client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await srv._client.start()
        body = json.loads(await srv.list_agents())
        assert "result" in body
        assert "_pending_messages" in body
        assert "_pending_task_events" in body
    finally:
        await srv._client.stop()
        srv._client = None


async def test_pending_messages_shows_then_drains_on_get_messages(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker
    client_a = BrokerClient(broker_url=url, api_key=key_a)
    srv._client = BrokerClient(broker_url=url, api_key=key_b)
    try:
        await client_a.start()
        await srv._client.start()

        # alice sends two messages to bob; bob's WS queue grows
        await client_a.send_message(to_agent=agent_b.id, content="m1")
        await client_a.send_message(to_agent=agent_b.id, content="m2")

        # wait for delivery
        for _ in range(50):
            if srv._client.pending_messages >= 2:
                break
            await asyncio.sleep(0.02)

        # any tool call should now report _pending_messages: 2
        body = json.loads(await srv.list_agents())
        assert body["_pending_messages"] == 2

        # get_messages drains; the interceptor sees the post-drain count = 0
        body = json.loads(await srv.get_messages())
        assert len(body["result"]) == 2
        assert body["_pending_messages"] == 0
    finally:
        await client_a.stop()
        await srv._client.stop()
        srv._client = None


async def test_pending_task_events_drained_by_list_tasks(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker
    client_a = BrokerClient(broker_url=url, api_key=key_a)
    srv._client = BrokerClient(broker_url=url, api_key=key_b)
    try:
        await client_a.start()
        await srv._client.start()

        # alice creates a task assigned to bob; bob's task_event queue grows
        await client_a.create_task(title="for bob", assigned_to=agent_b.id)

        for _ in range(50):
            if srv._client.pending_task_events >= 1:
                break
            await asyncio.sleep(0.02)

        body = json.loads(await srv.report_status("idle"))
        assert body["_pending_task_events"] >= 1

        body = json.loads(await srv.list_tasks())
        assert body["_pending_task_events"] == 0
    finally:
        await client_a.stop()
        await srv._client.stop()
        srv._client = None
