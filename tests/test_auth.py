from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient

from mab.broker.auth import (
    API_KEY_PREFIX,
    generate_api_key,
    get_current_agent,
    hash_api_key,
)
from mab.broker.db import Database
from mab.broker.keygen import NameConflict, gen_key
from mab.shared.models import Agent


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "test.db")
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


def test_hash_deterministic_and_unique():
    assert hash_api_key("a") == hash_api_key("a")
    assert hash_api_key("a") != hash_api_key("b")


def test_generate_api_key_format():
    k = generate_api_key()
    assert k.startswith(API_KEY_PREFIX)
    assert len(k) > len(API_KEY_PREFIX)


async def test_gen_key_creates_agent_with_correct_hash(db: Database):
    key, agent = await gen_key(db, name="w1", machine_id="m", capabilities=["py"])
    assert agent.name == "w1"
    assert agent.status == "offline"
    found = await db.get_agent_by_api_key_hash(hash_api_key(key))
    assert found is not None
    assert found.id == agent.id


async def test_gen_key_name_conflict_raises(db: Database):
    await gen_key(db, name="dup")
    with pytest.raises(NameConflict):
        await gen_key(db, name="dup")


async def test_gen_key_auto_suffix(db: Database):
    _, a1 = await gen_key(db, name="w")
    _, a2 = await gen_key(db, name="w", auto_suffix=True)
    _, a3 = await gen_key(db, name="w", auto_suffix=True)
    assert (a1.name, a2.name, a3.name) == ("w", "w-2", "w-3")


def _build_test_app(db: Database) -> FastAPI:
    app = FastAPI()
    app.state.db = db

    @app.get("/whoami")
    async def whoami(agent: Agent = Depends(get_current_agent)) -> dict[str, str]:
        return {"id": agent.id, "name": agent.name}

    return app


async def test_get_current_agent_accepts_valid_key(db: Database):
    key, agent = await gen_key(db, name="w1")
    app = _build_test_app(db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/whoami", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        assert r.json() == {"id": agent.id, "name": agent.name}


async def test_get_current_agent_rejects_bad_key(db: Database):
    await gen_key(db, name="w1")
    app = _build_test_app(db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/whoami", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


async def test_get_current_agent_rejects_missing_header(db: Database):
    app = _build_test_app(db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/whoami")
        assert r.status_code == 401
