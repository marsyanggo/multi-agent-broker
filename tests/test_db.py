from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from mab.broker.db import Database, _iso
from mab.shared.models import utc_now


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "test.db")
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


async def test_register_and_get_agent(db: Database):
    agent = await db.register_agent(
        name="builder-1",
        machine_id="laptop",
        capabilities=["python"],
        api_key_hash="h1",
    )
    fetched = await db.get_agent(agent.id)
    assert fetched is not None
    assert fetched.name == "builder-1"
    assert fetched.capabilities == ["python"]


async def test_duplicate_agent_name_fails(db: Database):
    await db.register_agent(
        name="dup", machine_id="m", capabilities=[], api_key_hash="h"
    )
    with pytest.raises(Exception):
        await db.register_agent(
            name="dup", machine_id="m", capabilities=[], api_key_hash="h2"
        )


async def test_atomic_task_claim(db: Database):
    a1 = await db.register_agent(
        name="w1", machine_id="m", capabilities=[], api_key_hash="h1"
    )
    a2 = await db.register_agent(
        name="w2", machine_id="m", capabilities=[], api_key_hash="h2"
    )
    task = await db.create_task(title="t", created_by=a1.id)

    r1, r2 = await asyncio.gather(
        db.claim_task(task_id=task.id, agent_id=a1.id),
        db.claim_task(task_id=task.id, agent_id=a2.id),
    )

    successes = [r for r in (r1, r2) if r is not None]
    assert len(successes) == 1
    assert successes[0].status == "assigned"


async def test_update_task_sets_completed_at(db: Database):
    a1 = await db.register_agent(
        name="creator", machine_id="m", capabilities=[], api_key_hash="h"
    )
    task = await db.create_task(title="t", created_by=a1.id)
    updated = await db.update_task(
        task.id, status="completed", result="done", note="all good"
    )
    assert updated is not None
    assert updated.status == "completed"
    assert updated.completed_at is not None
    assert updated.notes == ["all good"]


async def test_message_ttl_cleanup(db: Database):
    a1 = await db.register_agent(
        name="s", machine_id="m", capabilities=[], api_key_hash="h1"
    )
    a2 = await db.register_agent(
        name="r", machine_id="m", capabilities=[], api_key_hash="h2"
    )
    await db.create_message(from_agent=a1.id, to_agent=a2.id, content="recent")
    old = await db.create_message(from_agent=a1.id, to_agent=a2.id, content="old")
    await db.conn.execute(
        "UPDATE messages SET created_at = ? WHERE id = ?",
        (_iso(utc_now() - timedelta(days=10)), old.id),
    )
    await db.conn.commit()

    deleted = await db.cleanup_old_messages(ttl_days=7)
    assert deleted == 1

    remaining = await db.get_messages_for(a2.id)
    assert len(remaining) == 1
    assert remaining[0].content == "recent"


async def test_message_only_undelivered_filter(db: Database):
    a1 = await db.register_agent(
        name="s2", machine_id="m", capabilities=[], api_key_hash="h1"
    )
    a2 = await db.register_agent(
        name="r2", machine_id="m", capabilities=[], api_key_hash="h2"
    )
    m1 = await db.create_message(from_agent=a1.id, to_agent=a2.id, content="seen")
    await db.create_message(from_agent=a1.id, to_agent=a2.id, content="unseen")
    await db.mark_delivered(m1.id)

    undelivered = await db.get_messages_for(a2.id, only_undelivered=True)
    assert len(undelivered) == 1
    assert undelivered[0].content == "unseen"
