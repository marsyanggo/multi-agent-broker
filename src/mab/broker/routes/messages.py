from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mab.broker.auth import get_current_agent, get_db
from mab.broker.db import Database
from mab.broker.websocket import WebSocketHub, get_hub
from mab.shared.models import Agent, ContentType, Message, utc_now

router = APIRouter(prefix="/api/v1/messages", tags=["messages"])


class SendMessageRequest(BaseModel):
    to_agent: str
    content: str
    content_type: ContentType = "text/plain"
    reply_to: str | None = None


@router.post("", response_model=Message, status_code=201)
async def send_message(
    body: SendMessageRequest,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
    hub: Annotated[WebSocketHub, Depends(get_hub)],
) -> Message:
    if await db.get_agent(body.to_agent) is None:
        raise HTTPException(status_code=404, detail="recipient agent not found")
    msg = await db.create_message(
        from_agent=me.id,
        to_agent=body.to_agent,
        content=body.content,
        content_type=body.content_type,
        reply_to=body.reply_to,
    )
    delivered = await hub.try_deliver_message(msg)
    if delivered:
        msg.delivered = True
        msg.delivered_at = utc_now()
    return msg


@router.get("", response_model=list[Message])
async def list_messages(
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
    only_undelivered: bool = False,
    from_agent: str | None = None,
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    mark_delivered: bool = True,
) -> list[Message]:
    msgs = await db.get_messages_for(
        me.id,
        only_undelivered=only_undelivered,
        from_agent=from_agent,
        since=since,
        limit=limit,
    )
    if mark_delivered:
        now = utc_now()
        for m in msgs:
            if not m.delivered:
                await db.mark_delivered(m.id)
                m.delivered = True
                m.delivered_at = now
    return msgs
