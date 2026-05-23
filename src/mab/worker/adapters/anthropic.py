"""Anthropic API adapter — talks to /v1/messages directly via httpx (no SDK).

Configuration via constructor args; `ANTHROPIC_API_KEY` env var honoured if
`api_key` isn't passed. Pass a custom `transport=httpx.MockTransport(...)`
for testing.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from mab.shared.models import Task
from mab.worker.adapters.base import LLMAdapter, LLMError


class AnthropicAdapter(LLMAdapter):
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 1024,
        temperature: float | None = None,
        system_prompt: str | None = None,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def setup(self) -> None:
        if not self.api_key:
            raise LLMError(
                "AnthropicAdapter: api_key empty (set ANTHROPIC_API_KEY or pass --api-key)"
            )
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "headers": {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._http = httpx.AsyncClient(**kwargs)

    async def teardown(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def run_task(self, task: Task) -> str:
        if self._http is None:
            raise LLMError("AnthropicAdapter: setup() not called")
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": task.description}],
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.system_prompt:
            body["system"] = self.system_prompt
        try:
            r = await self._http.post("/v1/messages", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"Anthropic HTTP error: {e}") from e
        if r.status_code != 200:
            raise LLMError(
                f"Anthropic API HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            data = r.json()
            blocks = data.get("content", [])
            if not isinstance(blocks, list):
                raise LLMError(f"Anthropic content block list missing: {data}")
            text_parts = [
                b["text"]
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = "".join(text_parts).strip()
            if not text:
                raise LLMError(
                    f"Anthropic returned no text content: {data}"
                )
            return text
        except (ValueError, KeyError, TypeError) as e:
            raise LLMError(f"Malformed Anthropic response: {e}") from e
