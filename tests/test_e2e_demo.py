from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_BIN = PROJECT_ROOT / ".venv" / "bin"
BROKER_BIN = str(VENV_BIN / "mab-broker")
AGENT_BIN = str(VENV_BIN / "mab-agent")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"broker not ready at {url}")


def _gen_key(db_path: Path, name: str) -> str:
    env = {**os.environ, "MAB_DB_PATH": str(db_path)}
    res = subprocess.run(
        [BROKER_BIN, "gen-key", "--name", name],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    m = re.search(r"mab-ak-[A-Za-z0-9_-]+", res.stdout)
    assert m, f"no key in stdout: {res.stdout!r}"
    return m.group(0)


@pytest.fixture
def live_setup(tmp_path: Path):
    db_path = tmp_path / "demo.db"
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    key_a = _gen_key(db_path, "alice")
    key_b = _gen_key(db_path, "bob")

    env = {**os.environ, "MAB_DB_PATH": str(db_path)}
    broker = subprocess.Popen(
        [BROKER_BIN, "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_health(url)
        yield url, key_a, key_b
    finally:
        broker.terminate()
        try:
            broker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            broker.kill()
            broker.wait()


def _agent_params(url: str, key: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=AGENT_BIN,
        args=["--broker-url", url, "--api-key", key],
    )


async def _call(
    session: ClientSession, name: str, args: dict | None = None
) -> dict:
    res = await session.call_tool(name, args or {})
    return json.loads(res.content[0].text)


async def test_two_agents_see_each_other(live_setup):
    url, key_a, key_b = live_setup
    async with stdio_client(_agent_params(url, key_a)) as (ra, wa):
        async with ClientSession(ra, wa) as sa:
            await sa.initialize()
            async with stdio_client(_agent_params(url, key_b)) as (rb, wb):
                async with ClientSession(rb, wb) as sb:
                    await sb.initialize()
                    await asyncio.sleep(0.5)
                    body = await _call(sa, "list_agents")
                    names = {a["name"] for a in body["result"]}
                    assert names == {"alice", "bob"}


async def test_send_and_receive_messages(live_setup):
    url, key_a, key_b = live_setup
    async with stdio_client(_agent_params(url, key_a)) as (ra, wa):
        async with ClientSession(ra, wa) as sa:
            await sa.initialize()
            async with stdio_client(_agent_params(url, key_b)) as (rb, wb):
                async with ClientSession(rb, wb) as sb:
                    await sb.initialize()
                    await asyncio.sleep(0.3)

                    agents_body = await _call(sa, "list_agents")
                    bob_id = next(
                        a["id"]
                        for a in agents_body["result"]
                        if a["name"] == "bob"
                    )

                    send_body = await _call(
                        sa,
                        "send_message",
                        {"to_agent": bob_id, "content": "hi bob"},
                    )
                    assert send_body["result"]["delivered"] is True

                    await asyncio.sleep(0.3)
                    msgs_body = await _call(sb, "get_messages")
                    msgs = msgs_body["result"]
                    assert any(m["content"] == "hi bob" for m in msgs)

                    # The interceptor should report 0 pending after drain
                    assert msgs_body["_pending_messages"] == 0


async def test_full_task_lifecycle(live_setup):
    url, key_a, key_b = live_setup
    async with stdio_client(_agent_params(url, key_a)) as (ra, wa):
        async with ClientSession(ra, wa) as sa:
            await sa.initialize()
            async with stdio_client(_agent_params(url, key_b)) as (rb, wb):
                async with ClientSession(rb, wb) as sb:
                    await sb.initialize()
                    await asyncio.sleep(0.3)

                    created = (
                        await _call(
                            sa,
                            "create_task",
                            {"title": "fix bug", "priority": "high"},
                        )
                    )["result"]
                    task_id = created["id"]
                    assert created["status"] == "pending"

                    listed = (
                        await _call(sb, "list_tasks", {"status": "pending"})
                    )["result"]
                    assert any(t["id"] == task_id for t in listed)

                    claimed = (
                        await _call(sb, "claim_task", {"task_id": task_id})
                    )["result"]
                    assert claimed["status"] == "assigned"

                    done = (
                        await _call(
                            sb,
                            "update_task",
                            {
                                "task_id": task_id,
                                "status": "completed",
                                "result": "ok",
                                "note": "done in 5min",
                            },
                        )
                    )["result"]
                    assert done["status"] == "completed"
                    assert done["completed_at"] is not None
                    assert done["notes"] == ["done in 5min"]


async def test_offline_backfill(live_setup):
    url, key_a, key_b = live_setup

    # alice sends to bob while bob is offline
    async with stdio_client(_agent_params(url, key_a)) as (ra, wa):
        async with ClientSession(ra, wa) as sa:
            await sa.initialize()
            await asyncio.sleep(0.3)
            agents = (await _call(sa, "list_agents"))["result"]
            bob_id = next(a["id"] for a in agents if a["name"] == "bob")
            sent = (
                await _call(
                    sa,
                    "send_message",
                    {"to_agent": bob_id, "content": "for-later"},
                )
            )["result"]
            assert sent["delivered"] is False

    # bob comes online; should receive the backfill
    async with stdio_client(_agent_params(url, key_b)) as (rb, wb):
        async with ClientSession(rb, wb) as sb:
            await sb.initialize()
            await asyncio.sleep(0.5)
            msgs = (await _call(sb, "get_messages"))["result"]
            assert any(m["content"] == "for-later" for m in msgs)
