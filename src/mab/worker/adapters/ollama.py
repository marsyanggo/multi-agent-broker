"""Ollama adapter — works for both local Ollama (http://localhost:11434) and
Ollama Cloud (https://ollama.com). Uses /api/chat (more LLM-style) directly
via httpx; no SDK dependency.

Configuration via constructor args; `OLLAMA_API_KEY` env var honoured for
Ollama Cloud auth. Pass `transport=httpx.MockTransport(...)` for testing.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from mab.shared.models import Task
from mab.worker.adapters.base import LLMAdapter, LLMError


class OllamaAdapter(LLMAdapter):
    name = "ollama"

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        api_key: str | None = None,
        system_prompt: str | None = None,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        # Local Ollama doesn't need auth; cloud requires it. Empty key = local.
        self.api_key = api_key or os.environ.get("OLLAMA_API_KEY", "")
        self.system_prompt = system_prompt
        self.timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def setup(self) -> None:
        headers: dict[str, str] = {"content-type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "headers": headers,
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
            raise LLMError("OllamaAdapter: setup() not called")
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": task.description})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        try:
            r = await self._http.post("/api/chat", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama HTTP error: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama API HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
            content = data["message"]["content"]
            if not isinstance(content, str):
                raise LLMError(f"Ollama message.content not a string: {data}")
            return content.strip()
        except (ValueError, KeyError, TypeError) as e:
            raise LLMError(f"Malformed Ollama response: {e}") from e
