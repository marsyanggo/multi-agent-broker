from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import sys
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP

from mab.mcp_server.broker_client import BrokerClient
from mab.shared.capabilities import derive_capabilities_from_model

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
)
log = logging.getLogger("mab.agent")


_client: BrokerClient | None = None


def _client_or_raise() -> BrokerClient:
    if _client is None:
        raise RuntimeError("broker client not initialized")
    return _client


def _to_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _attach_pending(text_result: str) -> str:
    # Wrap every tool response so the LLM sees how many messages / task events
    # are sitting in the local queue waiting to be pulled. This is the standing
    # solution to Claude Code being unable to receive push notifications: every
    # tool call doubles as a poke to call get_messages / list_tasks.
    try:
        inner: Any = json.loads(text_result)
    except json.JSONDecodeError:
        inner = text_result
    client = _client
    return _to_json(
        {
            "result": inner,
            "_pending_messages": client.pending_messages if client else 0,
            "_pending_task_events": client.pending_task_events if client else 0,
        }
    )


def _with_pending(
    fn: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[str]]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        result = await fn(*args, **kwargs)
        return _attach_pending(result)

    return wrapper


mcp = FastMCP("mab-agent")


@mcp.tool()
@_with_pending
async def list_agents(status: str | None = None) -> str:
    """List known agents. Optional status filter: online, busy, idle, offline."""
    agents = await _client_or_raise().list_agents(status=status)  # type: ignore[arg-type]
    return _to_json([a.model_dump(mode="json") for a in agents])


@mcp.tool()
@_with_pending
async def get_agent_info(agent_id: str) -> str:
    """Get details about an agent by ID."""
    agent = await _client_or_raise().get_agent(agent_id)
    if agent is None:
        return _to_json({"error": "not found", "agent_id": agent_id})
    return _to_json(agent.model_dump(mode="json"))


@mcp.tool()
@_with_pending
async def report_status(status: str) -> str:
    """Update my own status. One of: online, busy, idle, offline."""
    if status not in ("online", "busy", "idle", "offline"):
        return _to_json({"error": "invalid status", "valid": ["online", "busy", "idle", "offline"]})
    agent = await _client_or_raise().report_status(status)  # type: ignore[arg-type]
    return _to_json(agent.model_dump(mode="json"))


@mcp.tool()
@_with_pending
async def send_message(
    to_agent: str,
    content: str,
    content_type: str = "text/plain",
    reply_to: str | None = None,
) -> str:
    """Send a direct message to another agent. content_type: text/plain, text/markdown, application/json."""
    msg = await _client_or_raise().send_message(
        to_agent=to_agent,
        content=content,
        content_type=content_type,  # type: ignore[arg-type]
        reply_to=reply_to,
    )
    return _to_json(msg.model_dump(mode="json"))


@mcp.tool()
@_with_pending
async def get_messages() -> str:
    """Pull messages addressed to me. Drains my local queue; falls back to REST if disconnected."""
    client = _client_or_raise()
    msgs = client.drain_messages()
    if not msgs and not client.is_connected:
        msgs = await client.fetch_messages(only_undelivered=True)
    return _to_json([m.model_dump(mode="json") for m in msgs])


@mcp.tool()
@_with_pending
async def create_task(
    title: str,
    description: str = "",
    assigned_to: str | None = None,
    priority: str = "normal",
    required_all: list[str] | None = None,
    required_any: list[str] | None = None,
) -> str:
    """Create a task. assigned_to=null leaves it open. priority: low, normal, high, urgent.

    Capability routing: required_all = tags every claimer must have (AND).
    required_any = at least one of these tags required (OR). Both default to empty
    (any agent can claim). Common tags: model:<exact>, family:claude, tier:opus,
    provider:anthropic, plus free-form flags like vision, audio."""
    task = await _client_or_raise().create_task(
        title=title,
        description=description,
        assigned_to=assigned_to,
        priority=priority,  # type: ignore[arg-type]
        required_all=required_all,
        required_any=required_any,
    )
    return _to_json(task.model_dump(mode="json"))


@mcp.tool()
@_with_pending
async def claim_task(task_id: str) -> str:
    """Claim an open task."""
    try:
        task = await _client_or_raise().claim_task(task_id)
    except Exception as e:
        return _to_json({"error": str(e), "task_id": task_id})
    return _to_json(task.model_dump(mode="json"))


@mcp.tool()
@_with_pending
async def update_task(
    task_id: str,
    status: str | None = None,
    result: str | None = None,
    note: str | None = None,
) -> str:
    """Update a task I am assigned to. status: pending, assigned, in_progress, completed, failed, blocked."""
    try:
        task = await _client_or_raise().update_task(
            task_id,
            status=status,  # type: ignore[arg-type]
            result=result,
            note=note,
        )
    except Exception as e:
        return _to_json({"error": str(e), "task_id": task_id})
    return _to_json(task.model_dump(mode="json"))


@mcp.tool()
@_with_pending
async def delete_task(task_id: str) -> str:
    """Delete a task I created or am assigned to. Broadcasts task_event:deleted."""
    try:
        await _client_or_raise().delete_task(task_id)
    except Exception as e:
        return _to_json({"error": str(e), "task_id": task_id})
    return _to_json({"deleted": task_id})


@mcp.tool()
@_with_pending
async def list_tasks(
    status: str | None = None,
    assigned_to: str | None = None,
    created_by: str | None = None,
) -> str:
    """List tasks, optionally filtered by status / assignee / creator. Acknowledges any pending task_event notifications."""
    client = _client_or_raise()
    client.drain_task_events()
    tasks = await client.list_tasks(
        status=status,  # type: ignore[arg-type]
        assigned_to=assigned_to,
        created_by=created_by,
    )
    return _to_json([t.model_dump(mode="json") for t in tasks])


async def _serve(
    broker_url: str,
    api_key: str,
    *,
    model: str | None = None,
    extra_capabilities: list[str] | None = None,
) -> None:
    global _client
    _client = BrokerClient(broker_url=broker_url, api_key=api_key)
    await _client.start()

    if model or extra_capabilities:
        caps: list[str] = []
        if model:
            caps.extend(derive_capabilities_from_model(model))
        if extra_capabilities:
            caps.extend(extra_capabilities)
        # Dedup preserving order.
        seen: set[str] = set()
        deduped = [c for c in caps if not (c in seen or seen.add(c))]
        updated = await _client.update_capabilities(deduped)
        _client.agent = updated
        log.info("capabilities updated: %s", deduped)

    log.info(
        "mab-agent connected: name=%s id=%s",
        _client.agent.name if _client.agent else "?",
        _client.agent.id if _client.agent else "?",
    )
    try:
        await mcp.run_stdio_async()
    finally:
        await _client.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mab-agent")
    parser.add_argument(
        "--broker-url",
        default=os.environ.get("MAB_BROKER_URL", "http://localhost:8420"),
    )
    parser.add_argument("--api-key", default=os.environ.get("MAB_API_KEY"))
    parser.add_argument(
        "--model",
        default=os.environ.get("MAB_MODEL"),
        help="Model identifier (e.g. claude-opus-4-7). Auto-derives capability tags.",
    )
    parser.add_argument(
        "--capabilities",
        default=os.environ.get("MAB_CAPABILITIES", ""),
        help="Extra capability tags, comma-separated. Combined with --model derivations.",
    )
    args = parser.parse_args()

    if not args.api_key:
        print(
            "error: --api-key or MAB_API_KEY env required", file=sys.stderr
        )
        sys.exit(2)

    extra_caps = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    asyncio.run(
        _serve(
            args.broker_url,
            args.api_key,
            model=args.model,
            extra_capabilities=extra_caps or None,
        )
    )
