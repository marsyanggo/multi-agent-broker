from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

import httpx
import websockets

from mab.shared.models import (
    Agent,
    AgentStatus,
    ContentType,
    Message,
    Task,
    TaskPriority,
    TaskStatus,
)
from mab.shared.protocol import (
    AgentEventEnvelope,
    MessageEnvelope,
    TaskEventEnvelope,
    envelope_adapter,
)

log = logging.getLogger("mab.client")

_MAX_BACKOFF_SECONDS = 60.0
_CONNECT_TIMEOUT_SECONDS = 15.0


class BrokerClient:
    def __init__(
        self,
        *,
        broker_url: str,
        api_key: str,
        machine_id: str | None = None,
        heartbeat_interval: float = 30.0,
    ):
        self.broker_url = broker_url.rstrip("/")
        self.api_key = api_key
        self.machine_id = machine_id or socket.gethostname()
        self.heartbeat_interval = heartbeat_interval

        self._http = httpx.AsyncClient(
            base_url=self.broker_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        self._ws: Any = None
        self._message_queue: list[Message] = []
        self._task_event_queue: list[Task] = []
        self._connected_evt = asyncio.Event()
        self._stop_evt = asyncio.Event()
        self._ws_task: asyncio.Task[None] | None = None
        self.agent: Agent | None = None

    @property
    def ws_url(self) -> str:
        base = self.broker_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/api/v1/ws"

    @property
    def pending_count(self) -> int:
        return len(self._message_queue) + len(self._task_event_queue)

    @property
    def pending_messages(self) -> int:
        return len(self._message_queue)

    @property
    def pending_task_events(self) -> int:
        return len(self._task_event_queue)

    @property
    def is_connected(self) -> bool:
        return self._connected_evt.is_set()

    def drain_messages(self) -> list[Message]:
        out, self._message_queue = self._message_queue, []
        return out

    def drain_task_events(self) -> list[Task]:
        out, self._task_event_queue = self._task_event_queue, []
        return out

    async def start(self) -> None:
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        try:
            await asyncio.wait_for(
                self._connected_evt.wait(), timeout=_CONNECT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stop_evt.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._http.aclose()

    async def _run_ws_loop(self) -> None:
        backoff = 1.0
        while not self._stop_evt.is_set():
            hb_task: asyncio.Task[None] | None = None
            try:
                async with websockets.connect(
                    self.ws_url,
                    additional_headers={"Authorization": f"Bearer {self.api_key}"},
                    ping_interval=self.heartbeat_interval,
                    ping_timeout=self.heartbeat_interval * 2,
                ) as ws:
                    self._ws = ws
                    log.info("connected: %s", self.ws_url)
                    backoff = 1.0
                    hb_task = asyncio.create_task(self._heartbeat_loop(ws))
                    try:
                        await self._receive_loop(ws)
                    finally:
                        hb_task.cancel()
                        try:
                            await hb_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        hb_task = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ws error: %s; reconnect in %.1fs", e, backoff)
            finally:
                self._ws = None
                self._connected_evt.clear()

            if self._stop_evt.is_set():
                break
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

    async def _heartbeat_loop(self, ws: Any) -> None:
        # Application-layer text heartbeat. Broker only updates last_heartbeat
        # on receive_text(); protocol-level WS pings don't reach that handler,
        # so without this loop the DB heartbeat freezes after connect.
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await ws.send("hb")
            except Exception:
                return

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                env = envelope_adapter.validate_json(raw)
            except Exception as e:
                log.warning("malformed envelope: %s", e)
                continue

            if isinstance(env, MessageEnvelope):
                self._message_queue.append(env.payload.message)
            elif isinstance(env, TaskEventEnvelope):
                self._task_event_queue.append(env.payload.task)
            elif isinstance(env, AgentEventEnvelope):
                if env.to_agent is not None and env.payload.event == "online":
                    self.agent = env.payload.agent
                    self._connected_evt.set()

    # --- REST proxy methods ---

    async def list_agents(self, *, status: AgentStatus | None = None) -> list[Agent]:
        params = {"status": status} if status else {}
        r = await self._http.get("/api/v1/agents", params=params)
        r.raise_for_status()
        return [Agent.model_validate(a) for a in r.json()]

    async def get_agent(self, agent_id: str) -> Agent | None:
        r = await self._http.get(f"/api/v1/agents/{agent_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return Agent.model_validate(r.json())

    async def get_me(self) -> Agent:
        r = await self._http.get("/api/v1/agents/me")
        r.raise_for_status()
        return Agent.model_validate(r.json())

    async def report_status(self, status: AgentStatus) -> Agent:
        r = await self._http.patch("/api/v1/agents/me", json={"status": status})
        r.raise_for_status()
        return Agent.model_validate(r.json())

    async def update_capabilities(self, capabilities: list[str]) -> Agent:
        r = await self._http.patch(
            "/api/v1/agents/me", json={"capabilities": capabilities}
        )
        r.raise_for_status()
        return Agent.model_validate(r.json())

    async def send_message(
        self,
        *,
        to_agent: str,
        content: str,
        content_type: ContentType = "text/plain",
        reply_to: str | None = None,
    ) -> Message:
        body = {
            "to_agent": to_agent,
            "content": content,
            "content_type": content_type,
            "reply_to": reply_to,
        }
        r = await self._http.post("/api/v1/messages", json=body)
        r.raise_for_status()
        return Message.model_validate(r.json())

    async def fetch_messages(
        self, *, only_undelivered: bool = False, limit: int = 100
    ) -> list[Message]:
        params: dict[str, Any] = {"limit": limit}
        if only_undelivered:
            params["only_undelivered"] = "true"
        r = await self._http.get("/api/v1/messages", params=params)
        r.raise_for_status()
        return [Message.model_validate(m) for m in r.json()]

    async def create_task(
        self,
        *,
        title: str,
        description: str = "",
        assigned_to: str | None = None,
        priority: TaskPriority = "normal",
        required_all: list[str] | None = None,
        required_any: list[str] | None = None,
    ) -> Task:
        body: dict[str, Any] = {
            "title": title,
            "description": description,
            "assigned_to": assigned_to,
            "priority": priority,
        }
        if required_all:
            body["required_all"] = required_all
        if required_any:
            body["required_any"] = required_any
        r = await self._http.post("/api/v1/tasks", json=body)
        r.raise_for_status()
        return Task.model_validate(r.json())

    async def claim_task(self, task_id: str) -> Task:
        r = await self._http.post(f"/api/v1/tasks/{task_id}/claim")
        r.raise_for_status()
        return Task.model_validate(r.json())

    async def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        result: str | None = None,
        note: str | None = None,
    ) -> Task:
        body: dict[str, Any] = {}
        if status is not None:
            body["status"] = status
        if result is not None:
            body["result"] = result
        if note is not None:
            body["note"] = note
        r = await self._http.patch(f"/api/v1/tasks/{task_id}", json=body)
        r.raise_for_status()
        return Task.model_validate(r.json())

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        assigned_to: str | None = None,
        created_by: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if assigned_to:
            params["assigned_to"] = assigned_to
        if created_by:
            params["created_by"] = created_by
        r = await self._http.get("/api/v1/tasks", params=params)
        r.raise_for_status()
        return [Task.model_validate(t) for t in r.json()]

    async def get_task(self, task_id: str) -> Task | None:
        r = await self._http.get(f"/api/v1/tasks/{task_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return Task.model_validate(r.json())
