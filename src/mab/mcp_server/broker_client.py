from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

import httpx
import websockets

from mab.shared.models import (
    Agent,
    AgentSnapshot,
    AgentStatus,
    Channel,
    ChannelMessage,
    Context,
    ContentType,
    Message,
    Task,
    TaskPriority,
    TaskStatus,
)
from mab.shared.protocol import (
    AgentEventEnvelope,
    ChannelMessageEnvelope,
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
        # (event_name, task) so consumers can filter by event type — workers
        # care about "created", leads about "completed" / "failed", etc.
        self._task_event_queue: list[tuple[str, Task]] = []
        self._channel_message_queue: list[ChannelMessage] = []
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
    def pending_channel_messages(self) -> int:
        return len(self._channel_message_queue)

    @property
    def is_connected(self) -> bool:
        return self._connected_evt.is_set()

    def drain_messages(self) -> list[Message]:
        out, self._message_queue = self._message_queue, []
        return out

    def drain_task_events(self) -> list[tuple[str, Task]]:
        """Pop and return all queued (event_name, task) pairs."""
        out, self._task_event_queue = self._task_event_queue, []
        return out

    def drain_channel_messages(self) -> list[ChannelMessage]:
        out, self._channel_message_queue = self._channel_message_queue, []
        return out

    async def pop_one_task_event(
        self,
        timeout: float,
        *,
        actionable_for_id: str | None = None,
        only_events: set[str] | None = None,
    ) -> Task | None:
        """Block until the next interesting task event arrives, or timeout.

        Pops and returns one Task. Filters:

        - `only_events`: if set, return tasks only when their event_name is in
          this set (e.g. `{"created"}` for workers waiting for new work,
          `{"completed","failed"}` for leads waiting for assignee outcomes).
          Default `{"created"}` if actionable_for_id is also given, else None.

        - `actionable_for_id`: if set, additionally requires the task to be
          either pending (any claimer may grab it, broker enforces caps) or
          assigned to that agent id. Filters out echoes for other agents.

        Returns the matching Task, or None on timeout / stop.
        """
        if only_events is None and actionable_for_id is not None:
            only_events = {"created"}
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            while self._task_event_queue:
                event_name, task = self._task_event_queue.pop(0)
                if only_events is not None and event_name not in only_events:
                    continue
                if actionable_for_id is None:
                    return task
                if task.status == "pending":
                    return task
                if task.status == "assigned" and task.assigned_to == actionable_for_id:
                    return task
                # Not actionable — drop and check next.
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0 or self._stop_evt.is_set():
                return None
            await asyncio.sleep(min(0.1, remaining))

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
            except websockets.exceptions.ConnectionClosed as e:
                # 4002 = broker superseded us with another connection using the
                # same agent_id. Reconnecting would just kick whoever took over
                # and start a flap loop. Give up — caller / supervisor decides
                # whether to respawn.
                if e.rcvd is not None and e.rcvd.code == 4002:
                    log.error(
                        "ws superseded (4002) — another mab-agent with the same "
                        "api-key took over. Stopping reconnect loop."
                    )
                    self._stop_evt.set()
                    break
                log.warning("ws closed: %s; reconnect in %.1fs", e, backoff)
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
                self._task_event_queue.append((env.payload.event, env.payload.task))
            elif isinstance(env, ChannelMessageEnvelope):
                self._channel_message_queue.append(env.payload.message)
            elif isinstance(env, AgentEventEnvelope):
                if env.to_agent is not None and env.payload.event == "online":
                    self.agent = env.payload.agent
                    self._connected_evt.set()

    # --- REST proxy methods ---

    async def list_agents(
        self, *, status: AgentStatus | None = None
    ) -> list[AgentSnapshot]:
        params = {"status": status} if status else {}
        r = await self._http.get("/api/v1/agents", params=params)
        r.raise_for_status()
        return [AgentSnapshot.model_validate(a) for a in r.json()]

    async def get_agent(self, agent_id: str) -> AgentSnapshot | None:
        r = await self._http.get(f"/api/v1/agents/{agent_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return AgentSnapshot.model_validate(r.json())

    async def get_me(self) -> AgentSnapshot:
        r = await self._http.get("/api/v1/agents/me")
        r.raise_for_status()
        return AgentSnapshot.model_validate(r.json())

    async def match_agents(
        self,
        *,
        required_all: list[str] | None = None,
        required_any: list[str] | None = None,
        available_only: bool = False,
        status: AgentStatus | None = "online",
    ) -> list[AgentSnapshot]:
        body: dict[str, Any] = {
            "required_all": required_all or [],
            "required_any": required_any or [],
            "available_only": available_only,
            "status": status,
        }
        r = await self._http.post("/api/v1/agents/match", json=body)
        r.raise_for_status()
        return [AgentSnapshot.model_validate(a) for a in r.json()]

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
        depends_on: list[str] | None = None,
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
        if depends_on:
            body["depends_on"] = depends_on
        r = await self._http.post("/api/v1/tasks", json=body)
        r.raise_for_status()
        return Task.model_validate(r.json())

    async def claim_task(self, task_id: str) -> Task:
        r = await self._http.post(f"/api/v1/tasks/{task_id}/claim")
        r.raise_for_status()
        return Task.model_validate(r.json())

    async def delete_task(self, task_id: str) -> None:
        r = await self._http.delete(f"/api/v1/tasks/{task_id}")
        r.raise_for_status()

    # --- Contexts ---

    async def create_context(
        self,
        *,
        name: str,
        content: str,
        content_type: ContentType = "text/markdown",
        task_id: str | None = None,
    ) -> Context:
        body: dict[str, Any] = {
            "name": name,
            "content": content,
            "content_type": content_type,
        }
        if task_id:
            body["task_id"] = task_id
        r = await self._http.post("/api/v1/contexts", json=body)
        r.raise_for_status()
        return Context.model_validate(r.json())

    async def list_contexts(
        self,
        *,
        name: str | None = None,
        created_by: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[Context]:
        params: dict[str, Any] = {"limit": limit}
        if name:
            params["name"] = name
        if created_by:
            params["created_by"] = created_by
        if task_id:
            params["task_id"] = task_id
        r = await self._http.get("/api/v1/contexts", params=params)
        r.raise_for_status()
        return [Context.model_validate(c) for c in r.json()]

    async def get_context(self, context_id: str) -> Context | None:
        r = await self._http.get(f"/api/v1/contexts/{context_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return Context.model_validate(r.json())

    async def update_context(
        self,
        context_id: str,
        *,
        content: str | None = None,
        name: str | None = None,
    ) -> Context:
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if name is not None:
            body["name"] = name
        r = await self._http.patch(f"/api/v1/contexts/{context_id}", json=body)
        r.raise_for_status()
        return Context.model_validate(r.json())

    async def delete_context(self, context_id: str) -> None:
        r = await self._http.delete(f"/api/v1/contexts/{context_id}")
        r.raise_for_status()

    # --- Channels ---

    async def create_channel(
        self,
        *,
        name: str,
        description: str = "",
        auto_suffix: bool = False,
    ) -> Channel:
        body: dict[str, Any] = {"name": name, "description": description}
        if auto_suffix:
            body["auto_suffix"] = True
        r = await self._http.post("/api/v1/channels", json=body)
        r.raise_for_status()
        return Channel.model_validate(r.json())

    async def list_channels(
        self, *, my_membership: bool = False
    ) -> list[Channel]:
        params: dict[str, Any] = {}
        if my_membership:
            params["my_membership"] = "true"
        r = await self._http.get("/api/v1/channels", params=params)
        r.raise_for_status()
        return [Channel.model_validate(c) for c in r.json()]

    async def get_channel(self, channel_id: str) -> dict | None:
        """Returns {'channel': Channel, 'members': [agent_id, ...]} or None."""
        r = await self._http.get(f"/api/v1/channels/{channel_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def delete_channel(self, channel_id: str) -> None:
        r = await self._http.delete(f"/api/v1/channels/{channel_id}")
        r.raise_for_status()

    async def join_channel(self, channel_id: str) -> dict:
        r = await self._http.post(f"/api/v1/channels/{channel_id}/join")
        r.raise_for_status()
        return r.json()

    async def leave_channel(self, channel_id: str) -> None:
        r = await self._http.post(f"/api/v1/channels/{channel_id}/leave")
        r.raise_for_status()

    async def post_to_channel(
        self,
        channel_id: str,
        *,
        content: str,
        content_type: ContentType = "text/plain",
    ) -> ChannelMessage:
        body = {"content": content, "content_type": content_type}
        r = await self._http.post(
            f"/api/v1/channels/{channel_id}/messages", json=body
        )
        r.raise_for_status()
        return ChannelMessage.model_validate(r.json())

    async def get_channel_messages(
        self,
        channel_id: str,
        *,
        since: str | None = None,
        limit: int = 100,
    ) -> list[ChannelMessage]:
        params: dict[str, Any] = {"limit": limit}
        if since:
            params["since"] = since
        r = await self._http.get(
            f"/api/v1/channels/{channel_id}/messages", params=params
        )
        r.raise_for_status()
        return [ChannelMessage.model_validate(m) for m in r.json()]

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
