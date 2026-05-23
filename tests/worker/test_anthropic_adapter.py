from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from mab.shared.models import Task
from mab.worker.adapters.anthropic import AnthropicAdapter
from mab.worker.adapters.base import LLMError


def _task(description: str = "Hello") -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id="t1",
        title="t",
        description=description,
        created_by="me",
        created_at=now,
        updated_at=now,
    )


def _mock(handler) -> httpx.AsyncBaseTransport:
    return httpx.MockTransport(handler)


async def test_anthropic_happy_path() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "the answer"}],
                "stop_reason": "end_turn",
            },
        )

    adapter = AnthropicAdapter(
        model="claude-sonnet-4-6", api_key="sk-test", transport=_mock(handler)
    )
    await adapter.setup()
    try:
        result = await adapter.run_task(_task("question?"))
    finally:
        await adapter.teardown()

    assert result == "the answer"
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"].get("x-api-key") == "sk-test"
    assert captured["headers"].get("anthropic-version") == "2023-06-01"
    assert captured["body"]["model"] == "claude-sonnet-4-6"
    assert captured["body"]["messages"] == [
        {"role": "user", "content": "question?"}
    ]


async def test_anthropic_includes_system_prompt() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}]}
        )

    adapter = AnthropicAdapter(
        model="claude-sonnet-4-6",
        api_key="sk-test",
        system_prompt="You are a worker. Be terse.",
        temperature=0.2,
        transport=_mock(handler),
    )
    await adapter.setup()
    try:
        await adapter.run_task(_task("ping"))
    finally:
        await adapter.teardown()

    assert captured["body"]["system"] == "You are a worker. Be terse."
    assert captured["body"]["temperature"] == 0.2


async def test_anthropic_concatenates_multiple_text_blocks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "part 1 "},
                    {"type": "text", "text": "part 2"},
                ]
            },
        )

    adapter = AnthropicAdapter(
        model="x", api_key="k", transport=_mock(handler)
    )
    await adapter.setup()
    try:
        result = await adapter.run_task(_task())
    finally:
        await adapter.teardown()
    assert result == "part 1 part 2"


async def test_anthropic_skips_non_text_blocks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "text", "text": "answer"},
                ]
            },
        )

    adapter = AnthropicAdapter(model="x", api_key="k", transport=_mock(handler))
    await adapter.setup()
    try:
        assert await adapter.run_task(_task()) == "answer"
    finally:
        await adapter.teardown()


async def test_anthropic_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    adapter = AnthropicAdapter(model="x", api_key="k", transport=_mock(handler))
    await adapter.setup()
    try:
        with pytest.raises(LLMError, match="429"):
            await adapter.run_task(_task())
    finally:
        await adapter.teardown()


async def test_anthropic_raises_on_empty_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": []})

    adapter = AnthropicAdapter(model="x", api_key="k", transport=_mock(handler))
    await adapter.setup()
    try:
        with pytest.raises(LLMError, match="no text"):
            await adapter.run_task(_task())
    finally:
        await adapter.teardown()


async def test_anthropic_raises_on_setup_without_key() -> None:
    import os

    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        adapter = AnthropicAdapter(model="x", api_key=None)
        with pytest.raises(LLMError, match="api_key"):
            await adapter.setup()
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


async def test_anthropic_run_without_setup_raises() -> None:
    adapter = AnthropicAdapter(model="x", api_key="k")
    with pytest.raises(LLMError, match="setup"):
        await adapter.run_task(_task())
