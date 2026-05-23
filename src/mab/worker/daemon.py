"""Standalone worker daemon for mab-broker.

WorkerDaemon connects to a broker, declares its capabilities, and runs an
async loop that pulls tasks via push-driven `wait_for_task` (catch-up scan on
startup for anything queued before the daemon came online) and dispatches each
to a pluggable `LLMAdapter`. Adapter failures and per-task timeouts mark the
task `failed` with a note; daemon survives and keeps looping.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from mab.mcp_server.broker_client import BrokerClient
from mab.shared.capabilities import derive_capabilities_from_model
from mab.shared.models import Task
from mab.worker.adapters.base import LLMAdapter, LLMError

log = logging.getLogger("mab.worker")


def _resolve_capabilities(
    model: str | None,
    extra_capabilities: list[str] | None,
) -> list[str]:
    caps: list[str] = []
    if model:
        caps.extend(derive_capabilities_from_model(model))
    if extra_capabilities:
        caps.extend(extra_capabilities)
    seen: set[str] = set()
    return [c for c in caps if not (c in seen or seen.add(c))]


class WorkerDaemon:
    """Long-running worker that processes broker tasks via a pluggable LLM adapter.

    Lifecycle:
      1. setup() the adapter
      2. PATCH /agents/me with declared capabilities (BEFORE WS connect, so
         broker uses fresh caps for routing as soon as we come online)
      3. WS connect to broker
      4. Catch-up scan: process any pre-existing assigned + matching pending
         tasks already in the broker
      5. Main loop: `wait_for_task` block, process when one arrives
      6. On stop() — finish current task if mid-flight, then teardown

    Failure modes:
      - LLMError from adapter → mark task failed with note, continue loop
      - asyncio.TimeoutError (per-task timeout) → mark task failed, continue
      - Unexpected exception → mark task failed, log, continue (don't crash daemon)
      - Broker unreachable for individual update_task calls → log + continue
        (BrokerClient handles WS reconnect on its own)
    """

    def __init__(
        self,
        *,
        broker_url: str,
        api_key: str,
        adapter: LLMAdapter,
        model: str | None = None,
        extra_capabilities: list[str] | None = None,
        per_task_timeout: float = 600.0,
        wait_for_task_timeout: float = 60.0,
    ):
        self.adapter = adapter
        self.per_task_timeout = per_task_timeout
        self.wait_for_task_timeout = wait_for_task_timeout
        self.client = BrokerClient(broker_url=broker_url, api_key=api_key)
        self._declared_caps = _resolve_capabilities(model, extra_capabilities)
        self._stop_evt = asyncio.Event()
        self._tasks_completed = 0
        self._tasks_failed = 0
        self._my_id: str | None = None

    @property
    def stats(self) -> dict[str, int]:
        return {
            "completed": self._tasks_completed,
            "failed": self._tasks_failed,
        }

    def request_stop(self) -> None:
        """Signal the main loop to stop after the current task completes.
        Safe to call from a signal handler. Idempotent."""
        if not self._stop_evt.is_set():
            log.info("stop requested; will exit after current task")
        self._stop_evt.set()

    async def start(self) -> None:
        """Run the daemon to completion (until stop is requested). Blocks."""
        log.info("worker daemon starting — adapter=%s", self.adapter.name)
        await self.adapter.setup()
        try:
            if self._declared_caps:
                await self.client.update_capabilities(self._declared_caps)
                log.info("declared capabilities: %s", self._declared_caps)

            await self.client.start()
            assert self.client.agent is not None, "broker handshake didn't populate agent"
            self._my_id = self.client.agent.id
            log.info(
                "connected as %s (id=%s)",
                self.client.agent.name,
                self._my_id,
            )

            await self._main_loop()
        finally:
            log.info("worker daemon stopping; stats=%s", self.stats)
            try:
                await self.client.stop()
            except Exception:
                log.exception("error while closing broker client")
            try:
                await self.adapter.teardown()
            except Exception:
                log.exception("error in adapter teardown")

    async def _main_loop(self) -> None:
        await self._catch_up_scan()
        while not self._stop_evt.is_set():
            try:
                task = await self.client.pop_one_task_event(
                    self.wait_for_task_timeout,
                    actionable_for_id=self._my_id,
                )
            except Exception:
                log.exception("wait_for_task failed; brief sleep then retry")
                await asyncio.sleep(1.0)
                continue
            if task is None:
                continue  # timeout — re-loop
            await self._process_task(task)

    async def _catch_up_scan(self) -> None:
        """Process any tasks already in broker state that we should pick up.
        Covers anything pushed before the daemon was online."""
        try:
            assigned = await self.client.list_tasks(
                assigned_to=self._my_id, status="assigned"
            )
            for task in assigned:
                if self._stop_evt.is_set():
                    return
                await self._process_task(task)

            pending = await self.client.list_tasks(status="pending")
            for task in pending:
                if self._stop_evt.is_set():
                    return
                await self._process_task(task)
        except Exception:
            log.exception("catch-up scan failed; continuing to main loop")

    async def _process_task(self, task: Task) -> None:
        # 1. Claim if still pending. Capability filter is enforced on broker.
        if task.status == "pending":
            try:
                task = await self.client.claim_task(task.id)
            except Exception as e:
                log.info("claim failed for task %s: %s", task.id, e)
                return

        if task.assigned_to != self._my_id:
            log.info(
                "task %s not assigned to me after claim; skipping", task.id
            )
            return

        # 2. Mark in_progress
        try:
            await self.client.update_task(
                task.id,
                status="in_progress",
                note=f"picked up by worker daemon ({self.adapter.name})",
            )
        except Exception as e:
            log.warning("failed to mark task %s in_progress: %s", task.id, e)
            return

        # 3. Run adapter under timeout
        try:
            result = await asyncio.wait_for(
                self.adapter.run_task(task),
                timeout=self.per_task_timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "task %s timed out after %.1fs", task.id, self.per_task_timeout
            )
            await self._safe_update(
                task.id,
                status="failed",
                note=f"timed out after {self.per_task_timeout}s",
            )
            self._tasks_failed += 1
            return
        except LLMError as e:
            log.warning("adapter error on task %s: %s", task.id, e)
            await self._safe_update(
                task.id, status="failed", note=f"adapter error: {e}"
            )
            self._tasks_failed += 1
            return
        except Exception as e:
            log.exception("unexpected error on task %s", task.id)
            await self._safe_update(
                task.id,
                status="failed",
                note=f"unexpected: {type(e).__name__}: {e}",
            )
            self._tasks_failed += 1
            return

        # 4. Mark completed
        try:
            await self.client.update_task(
                task.id, status="completed", result=result, note="done"
            )
            self._tasks_completed += 1
            log.info("task %s completed", task.id)
        except Exception as e:
            log.warning("failed to mark task %s completed: %s", task.id, e)

    async def _safe_update(self, task_id: str, **kwargs: Any) -> None:
        """Best-effort update_task — swallows exceptions. Use only on failure
        paths where we don't want the broker hiccup to mask the original
        error."""
        try:
            await self.client.update_task(task_id, **kwargs)
        except Exception as e:
            log.warning("failed to update task %s: %s", task_id, e)


def install_signal_handlers(daemon: WorkerDaemon) -> None:
    """Wire SIGTERM / SIGINT to `daemon.request_stop()`. Best-effort —
    silently no-ops on platforms (e.g. Windows) where signal handlers aren't
    available."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except (NotImplementedError, RuntimeError) as e:
            log.warning("could not install handler for %s: %s", sig.name, e)
