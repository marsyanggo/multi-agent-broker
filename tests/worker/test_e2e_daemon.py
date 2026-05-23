"""End-to-end scenarios that exercise daemon paths the unit-level tests in
test_daemon.py don't:

- broker-side capability rejection of a `claim_task` from a non-matching daemon
- task push that arrives AFTER catch-up scan completes (true push-driven path)
- graceful stop while a task is mid-flight (don't abandon in-flight work)
- larger burst (10 tasks) — queueing throughput smoke test

Reuses the `live_broker` fixture from `tests/conftest.py`.
"""
from __future__ import annotations

import asyncio

from mab.mcp_server.broker_client import BrokerClient
from mab.worker.adapters.mock import MockAdapter
from mab.worker.daemon import WorkerDaemon


async def _wait(pred, timeout: float = 5.0, tick: float = 0.05) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(tick)
    raise AssertionError("condition not met within timeout")


async def _run_until(daemon: WorkerDaemon, pred, timeout: float = 5.0) -> None:
    task = asyncio.create_task(daemon.start())
    try:
        await _wait(pred, timeout=timeout)
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


async def test_daemon_skips_pending_task_when_caps_dont_match(live_broker):
    """Daemon declares tier:opus via --model. Alice creates a task requiring
    tier:sonnet. The broker's claim endpoint returns 403; daemon swallows
    the error, skips, and never invokes the adapter."""
    url, (key_a, _agent_a), (key_b, _agent_b) = live_broker

    alice = BrokerClient(broker_url=url, api_key=key_a)
    await alice.start()
    try:
        await alice.create_task(
            title="not for opus",
            description="anyone with sonnet",
            required_all=["tier:sonnet"],
        )
    finally:
        await alice.stop()

    adapter = MockAdapter(response="should not be called")
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        model="claude-opus-4-7",  # → tier:opus, NOT tier:sonnet
        wait_for_task_timeout=0.3,
    )

    runner = asyncio.create_task(daemon.start())
    try:
        # Let catch-up scan run + a couple of wait_for_task cycles
        await asyncio.sleep(1.5)
    finally:
        daemon.request_stop()
        await asyncio.wait_for(runner, timeout=3.0)

    assert daemon.stats == {"completed": 0, "failed": 0}
    assert adapter.calls == []


async def test_daemon_processes_task_pushed_after_catchup_completes(live_broker):
    """Empty broker at startup → catch-up sees nothing → daemon enters
    wait_for_task block. Alice then dispatches; daemon picks it up via the
    push path, not via re-scanning."""
    url, (key_a, _agent_a), (key_b, agent_b) = live_broker

    adapter = MockAdapter(response="picked up")
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        wait_for_task_timeout=0.5,
    )

    runner = asyncio.create_task(daemon.start())
    try:
        await _wait(lambda: daemon.client.is_connected, timeout=3.0)
        # Grace period so catch-up scan finishes (empty DB so it's fast)
        await asyncio.sleep(0.5)
        assert daemon.stats == {"completed": 0, "failed": 0}

        # NOW dispatch — daemon must pick this up via wait_for_task push
        alice = BrokerClient(broker_url=url, api_key=key_a)
        await alice.start()
        try:
            await alice.create_task(
                title="post-catchup",
                description="reply hi",
                assigned_to=agent_b.id,
            )
        finally:
            await alice.stop()

        await _wait(lambda: daemon.stats["completed"] >= 1, timeout=5.0)
    finally:
        daemon.request_stop()
        await asyncio.wait_for(runner, timeout=3.0)

    assert daemon.stats == {"completed": 1, "failed": 0}
    assert adapter.calls[0].title == "post-catchup"


async def test_daemon_completes_in_flight_task_on_stop(live_broker):
    """request_stop() while the adapter is mid-execution: the current task
    should finish (not be abandoned). The stop flag is only consulted
    between loop iterations, not inside a running adapter call."""
    url, (key_a, _agent_a), (key_b, agent_b) = live_broker

    alice = BrokerClient(broker_url=url, api_key=key_a)
    await alice.start()
    try:
        await alice.create_task(
            title="slow task",
            description="will sleep mid-flight",
            assigned_to=agent_b.id,
        )
    finally:
        await alice.stop()

    adapter = MockAdapter(response="done", delay_seconds=0.4)
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        wait_for_task_timeout=0.3,
    )

    runner = asyncio.create_task(daemon.start())
    try:
        await _wait(lambda: len(adapter.calls) >= 1, timeout=3.0)
        # Adapter is now sleeping inside run_task — fire stop mid-flight
        daemon.request_stop()
        # Daemon must still finish the current task before exiting
        await asyncio.wait_for(runner, timeout=5.0)
    finally:
        if not runner.done():
            daemon.request_stop()
            await asyncio.wait_for(runner, timeout=3.0)

    assert daemon.stats == {"completed": 1, "failed": 0}


async def test_daemon_processes_burst_of_ten_tasks(live_broker):
    """Throughput smoke test — 10 tasks dispatched in a tight loop before
    the daemon starts. Daemon must complete all 10 via catch-up scan +
    queue drain without losing any."""
    url, (key_a, _agent_a), (key_b, agent_b) = live_broker

    alice = BrokerClient(broker_url=url, api_key=key_a)
    await alice.start()
    try:
        for i in range(10):
            await alice.create_task(
                title=f"burst-{i:02d}",
                description=f"#{i}",
                assigned_to=agent_b.id,
            )
    finally:
        await alice.stop()

    adapter = MockAdapter(response="done")
    daemon = WorkerDaemon(
        broker_url=url,
        api_key=key_b,
        adapter=adapter,
        wait_for_task_timeout=0.3,
    )

    await _run_until(
        daemon, lambda: daemon.stats["completed"] >= 10, timeout=15.0
    )

    assert daemon.stats == {"completed": 10, "failed": 0}
    titles = sorted(t.title for t in adapter.calls)
    assert titles == [f"burst-{i:02d}" for i in range(10)]
