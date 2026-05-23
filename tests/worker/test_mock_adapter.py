from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from mab.shared.models import Task
from mab.worker.adapters.base import LLMAdapter, LLMError, LLMTimeout
from mab.worker.adapters.mock import MockAdapter


def _task(description: str = "hello", task_id: str = "t1") -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        title="t",
        description=description,
        created_by="me",
        created_at=now,
        updated_at=now,
    )


async def test_mock_default_echoes_description() -> None:
    adapter = MockAdapter()
    result = await adapter.run_task(_task("hello world"))
    assert "hello world" in result


async def test_mock_fixed_string_response() -> None:
    adapter = MockAdapter(response="constant answer")
    result = await adapter.run_task(_task())
    assert result == "constant answer"


async def test_mock_callable_response_sync() -> None:
    adapter = MockAdapter(response=lambda t: t.description.upper())
    assert await adapter.run_task(_task("hello")) == "HELLO"


async def test_mock_callable_response_async() -> None:
    async def respond(task: Task) -> str:
        await asyncio.sleep(0)
        return f"async:{task.description}"

    adapter = MockAdapter(response=respond)
    assert await adapter.run_task(_task("xyz")) == "async:xyz"


async def test_mock_raises_configured_error() -> None:
    adapter = MockAdapter(error=LLMError("boom"))
    with pytest.raises(LLMError, match="boom"):
        await adapter.run_task(_task())


async def test_mock_raises_llm_timeout() -> None:
    adapter = MockAdapter(error=LLMTimeout("too slow"))
    with pytest.raises(LLMTimeout):
        await adapter.run_task(_task())


async def test_mock_records_calls_in_order() -> None:
    adapter = MockAdapter(response="ok")
    await adapter.run_task(_task("first", task_id="t1"))
    await adapter.run_task(_task("second", task_id="t2"))
    assert [c.id for c in adapter.calls] == ["t1", "t2"]


async def test_mock_delay_observed() -> None:
    adapter = MockAdapter(response="ok", delay_seconds=0.05)
    start = asyncio.get_event_loop().time()
    await adapter.run_task(_task())
    elapsed = asyncio.get_event_loop().time() - start
    assert 0.04 <= elapsed < 0.5  # generous upper bound for CI jitter


async def test_mock_setup_teardown_flags() -> None:
    adapter = MockAdapter(response="ok")
    assert not adapter.setup_called and not adapter.teardown_called

    await adapter.setup()
    assert adapter.setup_called

    await adapter.teardown()
    assert adapter.teardown_called


def test_mock_adapter_inherits_protocol() -> None:
    adapter = MockAdapter(response="ok")
    assert isinstance(adapter, LLMAdapter)
    assert adapter.name == "mock"


def test_abstract_base_cannot_instantiate() -> None:
    with pytest.raises(TypeError, match="abstract"):
        LLMAdapter()  # type: ignore[abstract]
