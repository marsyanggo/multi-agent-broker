"""Deterministic in-process adapter for tests and local smoke runs."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Union

from mab.shared.models import Task
from mab.worker.adapters.base import LLMAdapter

ResponseSpec = Union[str, Callable[[Task], str], Callable[[Task], "asyncio.Future[str]"]]


class MockAdapter(LLMAdapter):
    """Configurable test adapter.

    Use one of:
      - `response="literal text"` to return a fixed string
      - `response=lambda task: ...` for dynamic per-task responses (sync or async)
      - `error=Exception(...)` to simulate adapter failures

    `delay_seconds` simulates LLM call latency (default 0).
    `calls` accumulates every Task that ran through this adapter — handy in
    assertions to verify the daemon picked up the right tasks in order.
    """

    name = "mock"

    def __init__(
        self,
        *,
        response: ResponseSpec | None = None,
        error: BaseException | None = None,
        delay_seconds: float = 0.0,
    ):
        if response is None and error is None:
            response = lambda task: f"mock-echo: {task.description}"
        self._response = response
        self._error = error
        self._delay = delay_seconds
        self.calls: list[Task] = []
        self.setup_called = False
        self.teardown_called = False

    async def setup(self) -> None:
        self.setup_called = True

    async def teardown(self) -> None:
        self.teardown_called = True

    async def run_task(self, task: Task) -> str:
        self.calls.append(task)
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        spec = self._response
        if callable(spec):
            result = spec(task)
            if asyncio.iscoroutine(result):
                result = await result
            return str(result)
        return str(spec or "")
