from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import pytest_asyncio
import uvicorn
from fastapi import FastAPI

from mab.broker.db import Database
from mab.broker.keygen import gen_key
from mab.broker.routes import agents, contexts, messages, tasks
from mab.broker.websocket import WebSocketHub, router as ws_router


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def live_broker(tmp_path: Path):
    db_path = tmp_path / "test.db"

    seed_db = Database(db_path)
    await seed_db.connect()
    key_a, agent_a = await gen_key(seed_db, name="alice")
    key_b, agent_b = await gen_key(seed_db, name="bob")
    await seed_db.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        d = Database(db_path)
        await d.connect()
        app.state.db = d
        app.state.hub = WebSocketHub(d)
        try:
            yield
        finally:
            await d.close()

    fa = FastAPI(lifespan=lifespan)
    fa.include_router(agents.router)
    fa.include_router(messages.router)
    fa.include_router(tasks.router)
    fa.include_router(contexts.router)
    fa.include_router(ws_router)

    port = _free_port()
    config = uvicorn.Config(
        fa, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)

    try:
        yield (
            f"http://127.0.0.1:{port}",
            (key_a, agent_a),
            (key_b, agent_b),
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)
