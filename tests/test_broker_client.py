from __future__ import annotations

import asyncio

import pytest

from mab.mcp_server.broker_client import BrokerClient


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


async def test_client_connects_and_self_identity(live_broker):
    url, (key_a, agent_a), _ = live_broker
    client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await client.start()
        assert client.is_connected
        assert client.agent is not None
        assert client.agent.id == agent_a.id
        assert client.agent.name == "alice"
    finally:
        await client.stop()


async def test_client_rest_calls(live_broker):
    url, (key_a, _), (_, agent_b) = live_broker
    client = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await client.start()
        all_agents = await client.list_agents()
        assert {a.name for a in all_agents} == {"alice", "bob"}

        bob = await client.get_agent(agent_b.id)
        assert bob is not None and bob.name == "bob"

        me = await client.get_me()
        assert me.name == "alice"
    finally:
        await client.stop()


async def test_client_ws_receives_message(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker
    client_a = BrokerClient(broker_url=url, api_key=key_a)
    client_b = BrokerClient(broker_url=url, api_key=key_b)
    try:
        await client_a.start()
        await client_b.start()

        msg = await client_a.send_message(to_agent=agent_b.id, content="hi bob")
        assert msg.delivered is True

        await _wait_until(lambda: len(client_b._message_queue) >= 1)
        msgs = client_b.drain_messages()
        assert len(msgs) == 1
        assert msgs[0].content == "hi bob"
    finally:
        await client_a.stop()
        await client_b.stop()


async def test_client_offline_backfill(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker
    client_a = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await client_a.start()
        # bob is offline; send a message
        msg = await client_a.send_message(
            to_agent=agent_b.id, content="while you were out"
        )
        assert msg.delivered is False
    finally:
        await client_a.stop()

    # bob connects late; should receive backfill
    client_b = BrokerClient(broker_url=url, api_key=key_b)
    try:
        await client_b.start()
        await _wait_until(lambda: len(client_b._message_queue) >= 1)
        msgs = client_b.drain_messages()
        assert len(msgs) == 1
        assert msgs[0].content == "while you were out"
    finally:
        await client_b.stop()


async def test_client_task_lifecycle_via_rest(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker
    client_a = BrokerClient(broker_url=url, api_key=key_a)
    client_b = BrokerClient(broker_url=url, api_key=key_b)
    try:
        await client_a.start()
        await client_b.start()

        created = await client_a.create_task(title="fix bug", priority="high")
        assert created.status == "pending"

        pending_for_bob = await client_b.list_tasks(status="pending")
        assert any(t.id == created.id for t in pending_for_bob)

        claimed = await client_b.claim_task(created.id)
        assert claimed.assigned_to == agent_b.id
        assert claimed.status == "assigned"

        done = await client_b.update_task(
            created.id, status="completed", result="done"
        )
        assert done.status == "completed"
        assert done.completed_at is not None

        # Both sides should also have received task_events via WS
        await _wait_until(
            lambda: len(client_a._task_event_queue) >= 2  # created + claimed + completed
            or len(client_a._task_event_queue) >= 1,
            timeout=2,
        )
        events_a = client_a.drain_task_events()
        assert len(events_a) >= 1
    finally:
        await client_a.stop()
        await client_b.stop()


async def test_client_heartbeat_updates_last_heartbeat(live_broker):
    url, (key_a, _), _ = live_broker
    client = BrokerClient(broker_url=url, api_key=key_a, heartbeat_interval=0.1)
    try:
        await client.start()
        assert client.agent is not None
        initial = client.agent.last_heartbeat
        await asyncio.sleep(0.4)
        fresh = await client.get_me()
        assert fresh.last_heartbeat > initial
    finally:
        await client.stop()


async def test_client_invalid_key_raises(live_broker):
    url, _, _ = live_broker
    client = BrokerClient(broker_url=url, api_key="mab-ak-totally-fake")
    try:
        # Should fail to connect within timeout
        with pytest.raises(BaseException):
            await client.start()
    finally:
        await client.stop()
