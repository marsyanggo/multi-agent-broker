from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from mab.shared.models import Task
from mab.worker.adapters.base import LLMError
from mab.worker.adapters.ollama import OllamaAdapter


def _task(description: str = "ping") -> Task:
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


async def test_ollama_local_no_auth_header() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": "pong"}}
        )

    adapter = OllamaAdapter(
        model="llama3.3:70b", transport=_mock(handler)
    )  # local, no api_key
    await adapter.setup()
    try:
        result = await adapter.run_task(_task("ping"))
    finally:
        await adapter.teardown()

    assert result == "pong"
    assert "Authorization" not in captured["headers"]
    assert captured["url"].endswith("/api/chat")
    assert captured["body"]["model"] == "llama3.3:70b"
    assert captured["body"]["stream"] is False
    assert captured["body"]["messages"] == [{"role": "user", "content": "ping"}]


async def test_ollama_cloud_sends_bearer_auth() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200, json={"message": {"content": "answer"}}
        )

    adapter = OllamaAdapter(
        model="gpt-oss:120b-cloud",
        base_url="https://ollama.com",
        api_key="cloudkey",
        transport=_mock(handler),
    )
    await adapter.setup()
    try:
        await adapter.run_task(_task())
    finally:
        await adapter.teardown()

    assert captured["headers"].get("authorization") == "Bearer cloudkey"


async def test_ollama_system_prompt_prepended() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "x"}})

    adapter = OllamaAdapter(
        model="m",
        system_prompt="be concise",
        transport=_mock(handler),
    )
    await adapter.setup()
    try:
        await adapter.run_task(_task("hello"))
    finally:
        await adapter.teardown()

    assert captured["body"]["messages"] == [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "hello"},
    ]


async def test_ollama_strips_response_whitespace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"content": "  trimmed  \n"}}
        )

    adapter = OllamaAdapter(model="m", transport=_mock(handler))
    await adapter.setup()
    try:
        assert await adapter.run_task(_task()) == "trimmed"
    finally:
        await adapter.teardown()


async def test_ollama_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    adapter = OllamaAdapter(model="m", transport=_mock(handler))
    await adapter.setup()
    try:
        with pytest.raises(LLMError, match="500"):
            await adapter.run_task(_task())
    finally:
        await adapter.teardown()


async def test_ollama_raises_on_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    adapter = OllamaAdapter(model="m", transport=_mock(handler))
    await adapter.setup()
    try:
        with pytest.raises(LLMError, match="Malformed"):
            await adapter.run_task(_task())
    finally:
        await adapter.teardown()


async def test_ollama_run_without_setup_raises() -> None:
    adapter = OllamaAdapter(model="m")
    with pytest.raises(LLMError, match="setup"):
        await adapter.run_task(_task())
