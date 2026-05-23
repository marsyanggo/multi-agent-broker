from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from mab.broker.config import settings
from mab.broker.db import Database
from mab.broker.routes import agents, channels, contexts, messages, tasks
from mab.broker.websocket import WebSocketHub, router as ws_router

log = logging.getLogger("mab.app")

_CLEANUP_INTERVAL_SECONDS = 3600


async def _ttl_cleanup_loop(db: Database) -> None:
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        try:
            deleted = await db.cleanup_old_messages(
                ttl_days=settings.message_ttl_days
            )
            if deleted:
                log.info("cleaned %d expired messages", deleted)
        except Exception:
            log.exception("ttl cleanup failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db = Database(settings.db_path)
    await db.connect()
    app.state.db = db
    app.state.hub = WebSocketHub(db)

    cleanup_task = asyncio.create_task(_ttl_cleanup_loop(db))
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await db.close()


app = FastAPI(title="multi-agent-broker", version="0.1.0", lifespan=lifespan)

app.include_router(agents.router)
app.include_router(messages.router)
app.include_router(tasks.router)
app.include_router(contexts.router)
app.include_router(channels.router)
app.include_router(ws_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
