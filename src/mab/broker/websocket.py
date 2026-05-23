from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from mab.broker.auth import hash_api_key
from mab.broker.db import Database
from mab.shared.models import Agent, ChannelMessage, Message, Task, short_uuid
from mab.shared.protocol import (
    AgentEventEnvelope,
    AgentEventName,
    AgentEventPayload,
    ChannelMessageEnvelope,
    ChannelMessagePayload,
    MessageEnvelope,
    MessagePayload,
    TaskEventEnvelope,
    TaskEventName,
    TaskEventPayload,
)

log = logging.getLogger("mab.ws")

router = APIRouter()


class WebSocketHub:
    def __init__(self, db: Database):
        self.db = db
        self._conns: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    def is_online(self, agent_id: str) -> bool:
        return agent_id in self._conns

    async def connect(self, agent_id: str, ws: WebSocket) -> None:
        async with self._lock:
            existing = self._conns.pop(agent_id, None)
            self._conns[agent_id] = ws
        if existing is not None:
            try:
                await existing.close(code=4002, reason="superseded")
            except Exception:
                pass

    async def disconnect(self, agent_id: str, ws: WebSocket) -> bool:
        """Remove ws from registry. Returns True if this ws was the active one
        (so callers know whether to flip status); False means a newer ws had
        already taken over via supersede."""
        async with self._lock:
            current = self._conns.get(agent_id)
            if current is ws:
                self._conns.pop(agent_id, None)
                return True
            return False

    async def _send(self, agent_id: str, env: Any) -> bool:
        ws = self._conns.get(agent_id)
        if ws is None:
            return False
        try:
            await ws.send_json(env.model_dump(mode="json"))
            return True
        except Exception:
            log.warning("ws send failed for %s; dropping connection", agent_id)
            self._conns.pop(agent_id, None)
            return False

    async def emit_channel_message(
        self, message: ChannelMessage, members: Iterable[str]
    ) -> None:
        """Push a channel message to every online member of the channel
        (including the sender — sender's BrokerClient may filter own messages
        if it cares; broker doesn't distinguish)."""
        env = ChannelMessageEnvelope(
            id=short_uuid(),
            payload=ChannelMessagePayload(message=message),
        )
        for agent_id in members:
            await self._send(agent_id, env)

    async def try_deliver_message(self, message: Message) -> bool:
        env = MessageEnvelope(
            id=short_uuid(),
            from_agent=message.from_agent,
            to_agent=message.to_agent,
            payload=MessagePayload(message=message),
        )
        ok = await self._send(message.to_agent, env)
        if ok:
            await self.db.mark_delivered(message.id)
        return ok

    async def emit_task_event(
        self,
        event: TaskEventName,
        task: Task,
        *,
        extra_targets: Iterable[str] = (),
    ) -> None:
        env = TaskEventEnvelope(
            id=short_uuid(),
            payload=TaskEventPayload(event=event, task=task),
        )
        targets: set[str] = {task.created_by, *extra_targets}
        if task.assigned_to:
            targets.add(task.assigned_to)
        for agent_id in targets:
            await self._send(agent_id, env)

    async def emit_agent_event(
        self,
        event: AgentEventName,
        agent: Agent,
        *,
        exclude: str | None = None,
    ) -> None:
        env = AgentEventEnvelope(
            id=short_uuid(),
            payload=AgentEventPayload(event=event, agent=agent),
        )
        for agent_id in list(self._conns.keys()):
            if agent_id == exclude:
                continue
            await self._send(agent_id, env)


def get_hub(request: Request) -> WebSocketHub:
    return request.app.state.hub


@router.websocket("/api/v1/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    auth = websocket.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        await websocket.close(code=4001, reason="missing auth")
        return
    token = auth.split(" ", 1)[1].strip()

    db: Database = websocket.app.state.db
    hub: WebSocketHub = websocket.app.state.hub

    agent = await db.get_agent_by_api_key_hash(hash_api_key(token))
    if agent is None:
        await websocket.close(code=4001, reason="invalid auth")
        return

    await websocket.accept()
    await hub.connect(agent.id, websocket)
    await db.set_agent_status(agent.id, "online")
    await db.update_heartbeat(agent.id)

    fresh = await db.get_agent(agent.id)
    assert fresh is not None

    # Self-notification first so the client knows registration completed
    # before any further protocol activity.
    self_env = AgentEventEnvelope(
        id=short_uuid(),
        to_agent=agent.id,
        payload=AgentEventPayload(event="online", agent=fresh),
    )
    await websocket.send_json(self_env.model_dump(mode="json"))

    # Backfill any messages that arrived while offline.
    backlog = await db.get_messages_for(agent.id, only_undelivered=True)
    for m in backlog:
        env = MessageEnvelope(
            id=short_uuid(),
            from_agent=m.from_agent,
            to_agent=m.to_agent,
            payload=MessagePayload(message=m),
        )
        await websocket.send_json(env.model_dump(mode="json"))
        await db.mark_delivered(m.id)

    # Broadcast online to everyone else.
    await hub.emit_agent_event("online", fresh, exclude=agent.id)

    try:
        while True:
            await websocket.receive_text()
            await db.update_heartbeat(agent.id)
    except WebSocketDisconnect:
        pass
    finally:
        was_active = await hub.disconnect(agent.id, websocket)
        # Only flip status to offline if we were still the active connection.
        # If a newer WS already superseded us, it has already set status=online
        # and we must not clobber that.
        if was_active:
            await db.set_agent_status(agent.id, "offline")
            offline = await db.get_agent(agent.id)
            if offline is not None:
                await hub.emit_agent_event("offline", offline, exclude=agent.id)
