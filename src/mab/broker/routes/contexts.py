from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mab.broker.auth import get_current_agent, get_db
from mab.broker.db import Database
from mab.shared.models import Agent, Context, ContentType

router = APIRouter(prefix="/api/v1/contexts", tags=["contexts"])


class CreateContextRequest(BaseModel):
    name: str
    content: str
    content_type: ContentType = "text/markdown"
    task_id: str | None = None


class UpdateContextRequest(BaseModel):
    content: str | None = None
    name: str | None = None


@router.post("", response_model=Context, status_code=201)
async def create_context(
    body: CreateContextRequest,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> Context:
    return await db.create_context(
        name=body.name,
        content=body.content,
        content_type=body.content_type,
        created_by=me.id,
        task_id=body.task_id,
    )


@router.get("", response_model=list[Context])
async def list_contexts(
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
    name: str | None = None,
    created_by: str | None = None,
    task_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[Context]:
    return await db.list_contexts(
        name=name,
        created_by=created_by,
        task_id=task_id,
        limit=limit,
    )


@router.get("/{context_id}", response_model=Context)
async def get_context(
    context_id: str,
    db: Annotated[Database, Depends(get_db)],
    _me: Annotated[Agent, Depends(get_current_agent)],
) -> Context:
    ctx = await db.get_context(context_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="context not found")
    return ctx


@router.patch("/{context_id}", response_model=Context)
async def update_context(
    context_id: str,
    body: UpdateContextRequest,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> Context:
    ctx = await db.get_context(context_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="context not found")
    if ctx.created_by != me.id:
        raise HTTPException(
            status_code=403, detail="only the creator may update this context"
        )
    updated = await db.update_context(
        context_id, content=body.content, name=body.name
    )
    assert updated is not None
    return updated


@router.delete("/{context_id}", status_code=204)
async def delete_context(
    context_id: str,
    me: Annotated[Agent, Depends(get_current_agent)],
    db: Annotated[Database, Depends(get_db)],
) -> None:
    ctx = await db.get_context(context_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="context not found")
    if ctx.created_by != me.id:
        raise HTTPException(
            status_code=403, detail="only the creator may delete this context"
        )
    await db.delete_context(context_id)
