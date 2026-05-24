from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mab.broker.config import settings
from mab.broker.db import Database
from mab.broker.reaper import reaper_loop
from mab.broker.routes import agents, channels, contexts, dashboard, messages, tasks
from mab.broker.websocket import WebSocketHub, router as ws_router

_STATIC_DIR = Path(__file__).parent / "static"

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
    reaper_task = asyncio.create_task(reaper_loop(db, app.state.hub))
    try:
        yield
    finally:
        for task in (cleanup_task, reaper_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await db.close()


app = FastAPI(title="multi-agent-broker", version="0.1.0", lifespan=lifespan)

app.include_router(agents.router)
app.include_router(messages.router)
app.include_router(tasks.router)
app.include_router(contexts.router)
app.include_router(channels.router)
app.include_router(dashboard.router)
app.include_router(ws_router)

# Read-only web dashboard. Bundled into the broker — same port, same auth
# (Bearer token via Authorization header on /api/v1/dashboard/snapshot).
app.mount(
    "/dashboard",
    StaticFiles(directory=str(_STATIC_DIR), html=True),
    name="dashboard",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
