from __future__ import annotations

from datetime import datetime
from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mab.broker.auth import get_current_agent, get_db
from mab.broker.db import Database
from mab.broker.websocket import WebSocketHub, get_hub
from mab.shared.models import Agent, Channel, ChannelMessage, ContentType

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


class ChannelDetail(BaseModel):
    """Channel + its current member list. Returned from GET /channels/{id}."""

    channel: Channel
    members: list[str]


class CreateChannelRequest(BaseModel):
    name: str
    description: str = ""
    auto_suffix: bool = False  # if name collides, append -2, -3, ...


class PostMessageRequest(BaseModel):
    content: str
    content_type: ContentType = "text/plain"


@router.post("", response_model=Channel, status_code=201)
async def create_channel(
    body: CreateChannelRequest,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> Channel:
    name = body.name
    try:
        return await db.create_channel(
            name=name, description=body.description, created_by=me.id
        )
    except aiosqlite.IntegrityError:
        if not body.auto_suffix:
            raise HTTPException(
                status_code=409,
                detail=f"channel name '{name}' already exists",
            )
        # Auto-suffix: append -2, -3, ... until unique
        for n in range(2, 1000):
            candidate = f"{name}-{n}"
            try:
                return await db.create_channel(
                    name=candidate,
                    description=body.description,
                    created_by=me.id,
                )
            except aiosqlite.IntegrityError:
                continue
        raise HTTPException(
            status_code=409,
            detail=f"could not find a free suffix for '{name}'",
        )


@router.get("", response_model=list[Channel])
async def list_channels(
    db: Annotated[Database, Depends(get_db)],
    me: Annotated[Agent, Depends(get_current_agent)],
    my_membership: bool = False,
) -> list[Channel]:
    return await db.list_channels(member_id=me.id if my_membership else None)


@router.get("/{channel_id}", response_model=ChannelDetail)
async def get_channel(
    channel_id: str,
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
) -> ChannelDetail:
    ch = await db.get_channel(channel_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    members = await db.list_channel_members(channel_id)
    return ChannelDetail(channel=ch, members=members)


@router.delete("/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: str,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> None:
    ch = await db.get_channel(channel_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    if ch.created_by != me.id:
        raise HTTPException(
            status_code=403, detail="only the channel creator may delete it"
        )
    await db.delete_channel(channel_id)


@router.post("/{channel_id}/join", response_model=ChannelDetail)
async def join_channel(
    channel_id: str,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> ChannelDetail:
    ch = await db.get_channel(channel_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    await db.join_channel(channel_id, me.id)
    members = await db.list_channel_members(channel_id)
    return ChannelDetail(channel=ch, members=members)


@router.post("/{channel_id}/leave", status_code=204)
async def leave_channel(
    channel_id: str,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> None:
    ch = await db.get_channel(channel_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    await db.leave_channel(channel_id, me.id)


@router.post(
    "/{channel_id}/messages", response_model=ChannelMessage, status_code=201
)
async def post_channel_message(
    channel_id: str,
    body: PostMessageRequest,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
    hub: Annotated[WebSocketHub, Depends(get_hub)],
) -> ChannelMessage:
    ch = await db.get_channel(channel_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    if not await db.is_channel_member(channel_id, me.id):
        raise HTTPException(
            status_code=403, detail="must join channel before posting"
        )
    msg = await db.post_channel_message(
        channel_id=channel_id,
        from_agent=me.id,
        content=body.content,
        content_type=body.content_type,
    )
    members = await db.list_channel_members(channel_id)
    await hub.emit_channel_message(msg, members)
    return msg


@router.get("/{channel_id}/messages", response_model=list[ChannelMessage])
async def list_channel_messages(
    channel_id: str,
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[ChannelMessage]:
    ch = await db.get_channel(channel_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return await db.list_channel_messages(channel_id, since=since, limit=limit)
