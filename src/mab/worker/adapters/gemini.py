"""Google Gemini adapter — talks to Google AI Studio's generativelanguage
API directly via httpx (no SDK).

Configuration via constructor args; `GEMINI_API_KEY` env var honoured if
`api_key` isn't passed. Pass a custom `transport=httpx.MockTransport(...)`
for testing.

API reference:
  POST /v1beta/models/{model}:generateContent
  Authentication: x-goog-api-key header (preferred) or ?key= query param
  Request: {"contents": [...], "generationConfig": {...}, "systemInstruction": {...}}
  Response: {"candidates": [{"content": {"parts": [{"text": "..."}], "role": "model"}, ...}]}
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from mab.shared.models import Task
from mab.worker.adapters.base import LLMAdapter, LLMError


class GeminiAdapter(LLMAdapter):
    name = "gemini"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://generativelanguage.googleapis.com",
        # Default raised from 1024 to 8192 after demo11 (3-vendor Tokyo
        # itinerary comparison): the prior cap silently truncated
        # long-form outputs mid-sentence with no error. 8192 is well
        # under the Gemini 2.5 Flash output-token ceiling (~65K) and
        # comfortably fits 7-day itineraries, multi-page essays, etc.
        max_output_tokens: int = 8192,
        temperature: float | None = None,
        system_prompt: str | None = None,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def setup(self) -> None:
        if not self.api_key:
            raise LLMError(
                "GeminiAdapter: api_key empty (set GEMINI_API_KEY or pass --gemini-api-key)"
            )
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "headers": {
                "x-goog-api-key": self.api_key,
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
            raise LLMError("GeminiAdapter: setup() not called")

        body: dict[str, Any] = {
            "contents": [
                {"role": "user", "parts": [{"text": task.description}]}
            ],
            "generationConfig": {"maxOutputTokens": self.max_output_tokens},
        }
        if self.temperature is not None:
            body["generationConfig"]["temperature"] = self.temperature
        if self.system_prompt:
            body["systemInstruction"] = {"parts": [{"text": self.system_prompt}]}

        url = f"/v1beta/models/{self.model}:generateContent"
        try:
            r = await self._http.post(url, json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"Gemini HTTP error: {e}") from e
        if r.status_code != 200:
            raise LLMError(
                f"Gemini API HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            data = r.json()
            candidates = data.get("candidates", [])
            if not isinstance(candidates, list) or not candidates:
                raise LLMError(f"Gemini returned no candidates: {data}")
            first = candidates[0]
            content = first.get("content") or {}
            parts = content.get("parts", [])
            if not isinstance(parts, list):
                raise LLMError(f"Gemini parts not a list: {data}")
            text_parts = [
                p["text"] for p in parts if isinstance(p, dict) and "text" in p
            ]
            text = "".join(text_parts).strip()
            if not text:
                # Surface finishReason for safety / quota / length issues
                finish = first.get("finishReason", "?")
                raise LLMError(
                    f"Gemini returned no text content (finishReason={finish}): {data}"
                )
            return text
        except (ValueError, KeyError, TypeError) as e:
            raise LLMError(f"Malformed Gemini response: {e}") from e
