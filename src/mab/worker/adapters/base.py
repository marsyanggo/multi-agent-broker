"""LLM adapter contract for the worker daemon.

Each adapter takes a Task and returns the LLM's response as a string. Adapters
do NOT touch the broker — the daemon handles all broker interactions (claim,
update_task, etc.).

Subclass `LLMAdapter` and implement `run_task`. Override `setup` /
`teardown` if you need to manage persistent connections or probe an endpoint.
"""
from __future__ import annotations

import abc

from mab.shared.models import Task


class LLMError(Exception):
    """The adapter failed to obtain a result from the LLM. Daemon will mark
    the task `failed` and continue with the next one."""


class LLMTimeout(LLMError):
    """The LLM call exceeded the configured per-task timeout."""


class LLMAdapter(abc.ABC):
    """Pluggable LLM backend for the worker daemon."""

    # Short identifier — used in logs and as the value of the `--adapter`
    # CLI flag. Subclasses should set this as a class attribute.
    name: str = "base"

    async def setup(self) -> None:
        """Optional one-time initialisation. Probe the LLM endpoint, verify
        credentials, allocate persistent HTTP sessions, etc. Daemon calls
        this once before entering the main loop. Raise on unrecoverable
        config errors so the daemon exits with a clear message rather than
        silently failing every task."""
        return None

    async def teardown(self) -> None:
        """Optional cleanup. Daemon calls this on graceful shutdown
        (SIGTERM / SIGINT) — close HTTP sessions, kill subprocesses, etc."""
        return None

    @abc.abstractmethod
    async def run_task(self, task: Task) -> str:
        """Execute the task description and return the LLM's result as a
        single string. Raise `LLMError` (or any subclass) on failure;
        the daemon will mark the task `failed` with the exception message
        in the note."""
        raise NotImplementedError
