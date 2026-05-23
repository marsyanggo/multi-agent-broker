from __future__ import annotations

import asyncio

import pytest

from mab.mcp_server.broker_client import BrokerClient
from mab.shared.models import Task
from mab.worker.adapters.base import LLMError, LLMTimeout
from mab.worker.adapters.mock import MockAdapter
from mab.worker.daemon import WorkerDaemon, _resolve_capabilities


async def _run_until(daemon: WorkerDaemon, pred, timeout: float = 5.0) -> None:
    """Start daemon in background, stop when pred() turns true or timeout."""
    task = asyncio.create_task(daemon.start())
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if pred():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("daemon condition not met within timeout")
    finally:
        daemon.request_stop()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def test_daemon_processes_directly_assigned_task(live_broker):
    url, (key_a, _agent_a), (key_b, agent_b) = live_broker

    alice = BrokerClient(broker_url=url, api_key=key_a)
    await alice.start()
    try:
        await alice.create_task(
            title="hello", description="reply: hi", assigned_to=agent_b.id
        )
    finally:
        await alice.stop()

    adapter = MockAdapter(response="hi from mock")
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        wait_for_task_timeout=0.5,
    )

    await _run_until(daemon, lambda: daemon.stats["completed"] >= 1)

    assert daemon.stats == {"completed": 1, "failed": 0}
    assert len(adapter.calls) == 1
    assert adapter.calls[0].description == "reply: hi"


async def test_daemon_claims_pending_open_pool(live_broker):
    url, (key_a, _), (key_b, _agent_b) = live_broker

    alice = BrokerClient(broker_url=url, api_key=key_a)
    await alice.start()
    try:
        # Open pool — no assigned_to, no caps required
        await alice.create_task(title="any-one", description="solve x")
    finally:
        await alice.stop()

    adapter = MockAdapter(response="solved")
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        wait_for_task_timeout=0.5,
    )

    await _run_until(daemon, lambda: daemon.stats["completed"] >= 1)
    assert daemon.stats["completed"] == 1


async def test_daemon_marks_failed_on_adapter_error(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker

    alice = BrokerClient(broker_url=url, api_key=key_a)
    await alice.start()
    try:
        await alice.create_task(title="boom probe", assigned_to=agent_b.id)
    finally:
        await alice.stop()

    adapter = MockAdapter(error=LLMError("simulated llm failure"))
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        wait_for_task_timeout=0.5,
    )

    await _run_until(daemon, lambda: daemon.stats["failed"] >= 1)

    assert daemon.stats == {"completed": 0, "failed": 1}

    # Verify broker state matches
    probe = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await probe.start()
        tasks = await probe.list_tasks(status="failed", limit=5)
        assert len(tasks) == 1
        assert "simulated llm failure" in (tasks[0].notes[-1] if tasks[0].notes else "")
    finally:
        await probe.stop()


async def test_daemon_marks_failed_on_per_task_timeout(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker

    alice = BrokerClient(broker_url=url, api_key=key_a)
    await alice.start()
    try:
        await alice.create_task(title="slow probe", assigned_to=agent_b.id)
    finally:
        await alice.stop()

    adapter = MockAdapter(response="never seen", delay_seconds=10.0)
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        per_task_timeout=0.2,
        wait_for_task_timeout=0.5,
    )

    await _run_until(daemon, lambda: daemon.stats["failed"] >= 1, timeout=5.0)

    assert daemon.stats["failed"] == 1
    probe = BrokerClient(broker_url=url, api_key=key_a)
    try:
        await probe.start()
        tasks = await probe.list_tasks(status="failed", limit=5)
        assert any("timed out" in (t.notes[-1] if t.notes else "") for t in tasks)
    finally:
        await probe.stop()


async def test_daemon_continues_after_individual_failure(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker

    alice = BrokerClient(broker_url=url, api_key=key_a)
    await alice.start()
    try:
        # First task fails, second succeeds — daemon must process both.
        await alice.create_task(title="t1", description="FAIL", assigned_to=agent_b.id)
        await alice.create_task(title="t2", description="OK", assigned_to=agent_b.id)
    finally:
        await alice.stop()

    def respond(task: Task) -> str:
        if "FAIL" in task.description:
            raise LLMError("bad")
        return "good"

    adapter = MockAdapter(response=respond)
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        wait_for_task_timeout=0.5,
    )

    await _run_until(
        daemon,
        lambda: daemon.stats["completed"] + daemon.stats["failed"] >= 2,
    )
    assert daemon.stats == {"completed": 1, "failed": 1}


async def test_daemon_declares_capabilities_from_model(live_broker):
    url, _, (key_b, _agent_b) = live_broker

    adapter = MockAdapter(response="ok")
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        model="claude-opus-4-7",
        extra_capabilities=["vision"],
        wait_for_task_timeout=0.3,
    )

    # Start daemon briefly so it can PATCH /agents/me and WS-connect.
    task = asyncio.create_task(daemon.start())
    try:
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            if daemon.client.is_connected:
                break
            await asyncio.sleep(0.05)
    finally:
        daemon.request_stop()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # Re-fetch from broker to confirm caps applied
    probe = BrokerClient(broker_url=url, api_key=key_b)
    try:
        await probe.start()
        me = await probe.get_me()
        caps = set(me.capabilities)
        assert {"model:claude-opus-4-7", "family:claude", "tier:opus", "vision"} <= caps
    finally:
        await probe.stop()


async def test_daemon_setup_teardown_lifecycle(live_broker):
    url, _, (key_b, _) = live_broker

    adapter = MockAdapter(response="ok")
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        wait_for_task_timeout=0.3,
    )

    task = asyncio.create_task(daemon.start())
    await asyncio.sleep(0.2)
    daemon.request_stop()
    await asyncio.wait_for(task, timeout=3.0)

    assert adapter.setup_called
    assert adapter.teardown_called


async def test_daemon_stats_track_completion(live_broker):
    url, (key_a, _), (key_b, agent_b) = live_broker

    alice = BrokerClient(broker_url=url, api_key=key_a)
    await alice.start()
    try:
        for i in range(3):
            await alice.create_task(
                title=f"t{i}", description=f"task {i}", assigned_to=agent_b.id
            )
    finally:
        await alice.stop()

    adapter = MockAdapter(response="done")
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        wait_for_task_timeout=0.5,
    )

    await _run_until(daemon, lambda: daemon.stats["completed"] >= 3)
    assert daemon.stats == {"completed": 3, "failed": 0}


def test_resolve_capabilities_merges_model_and_extras() -> None:
    caps = _resolve_capabilities("gpt-oss:120b-cloud", ["custom-tag", "vision"])
    assert "model:gpt-oss:120b-cloud" in caps
    assert "host:cloud" in caps
    assert "tier:reasoning" in caps
    assert "custom-tag" in caps
    assert "vision" in caps


def test_resolve_capabilities_empty_when_no_inputs() -> None:
    assert _resolve_capabilities(None, None) == []
    assert _resolve_capabilities(None, []) == []


def test_resolve_capabilities_deduplicates() -> None:
    # If extra_capabilities repeats a derived tag, it's dedup'd.
    caps = _resolve_capabilities("claude-opus-4-7", ["tier:opus", "vision"])
    assert caps.count("tier:opus") == 1
    assert "vision" in caps
