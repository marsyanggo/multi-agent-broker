"""GeminiAdapter unit tests.

Uses httpx.MockTransport (built-in) — no real Google AI Studio call.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from mab.shared.models import Task
from mab.worker.adapters.base import LLMError
from mab.worker.adapters.gemini import GeminiAdapter


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


async def test_gemini_happy_path() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "the answer"}],
                            "role": "model",
                        },
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
            },
        )

    adapter = GeminiAdapter(
        model="gemini-2.5-flash", api_key="test-key", transport=_mock(handler)
    )
    await adapter.setup()
    try:
        result = await adapter.run_task(_task("question?"))
    finally:
        await adapter.teardown()

    assert result == "the answer"
    assert captured["url"].endswith("/v1beta/models/gemini-2.5-flash:generateContent")
    assert captured["headers"].get("x-goog-api-key") == "test-key"
    assert captured["body"]["contents"] == [
        {"role": "user", "parts": [{"text": "question?"}]}
    ]
    assert captured["body"]["generationConfig"]["maxOutputTokens"] == 8192


async def test_gemini_includes_system_prompt() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
        )

    adapter = GeminiAdapter(
        model="gemini-2.5-pro",
        api_key="key",
        system_prompt="You are terse.",
        transport=_mock(handler),
    )
    await adapter.setup()
    try:
        await adapter.run_task(_task("hi"))
    finally:
        await adapter.teardown()

    assert captured["body"]["systemInstruction"] == {
        "parts": [{"text": "You are terse."}]
    }


async def test_gemini_includes_temperature() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
        )

    adapter = GeminiAdapter(
        model="gemini-2.5-flash",
        api_key="key",
        temperature=0.2,
        transport=_mock(handler),
    )
    await adapter.setup()
    try:
        await adapter.run_task(_task("hi"))
    finally:
        await adapter.teardown()

    assert captured["body"]["generationConfig"]["temperature"] == 0.2


async def test_gemini_concatenates_multiple_parts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "first "},
                                {"text": "second"},
                            ]
                        }
                    }
                ]
            },
        )

    adapter = GeminiAdapter(
        model="gemini-2.5-flash", api_key="k", transport=_mock(handler)
    )
    await adapter.setup()
    try:
        result = await adapter.run_task(_task())
    finally:
        await adapter.teardown()
    assert result == "first second"


async def test_gemini_setup_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    adapter = GeminiAdapter(model="gemini-2.5-flash")
    with pytest.raises(LLMError, match="api_key empty"):
        await adapter.setup()


async def test_gemini_picks_up_env_key(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    adapter = GeminiAdapter(model="gemini-2.5-flash")
    assert adapter.api_key == "from-env"


async def test_gemini_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="API_KEY_INVALID")

    adapter = GeminiAdapter(
        model="gemini-2.5-flash", api_key="bad", transport=_mock(handler)
    )
    await adapter.setup()
    try:
        with pytest.raises(LLMError, match="HTTP 403"):
            await adapter.run_task(_task())
    finally:
        await adapter.teardown()


async def test_gemini_empty_response_surfaces_finish_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": []},
                        "finishReason": "SAFETY",
                    }
                ]
            },
        )

    adapter = GeminiAdapter(
        model="gemini-2.5-flash", api_key="k", transport=_mock(handler)
    )
    await adapter.setup()
    try:
        with pytest.raises(LLMError, match="finishReason=SAFETY"):
            await adapter.run_task(_task())
    finally:
        await adapter.teardown()


async def test_gemini_missing_candidates_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    adapter = GeminiAdapter(
        model="gemini-2.5-flash", api_key="k", transport=_mock(handler)
    )
    await adapter.setup()
    try:
        with pytest.raises(LLMError, match="no candidates"):
            await adapter.run_task(_task())
    finally:
        await adapter.teardown()
