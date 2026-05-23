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
    """List known agents (enriched with liveness). Optional status filter: online, busy, idle, offline.

    Each agent includes `last_heartbeat_age_seconds` and `is_stale` so a Lead Agent
    can tell who is really around: status="online" but is_stale=true means the WS
    hasn't sent heartbeat for >3x heartbeat_interval (likely hung)."""
    agents = await _client_or_raise().list_agents(status=status)  # type: ignore[arg-type]
    return _to_json([a.model_dump(mode="json") for a in agents])


@mcp.tool()
@_with_pending
async def match_agents(
    required_all: list[str] | None = None,
    required_any: list[str] | None = None,
    available_only: bool = False,
    status: str | None = "online",
) -> str:
    """Find agents matching a capability spec (roster check before dispatch).

    Use this before create_task to confirm someone can take the work:
    - required_all: tags every match must have (AND).
    - required_any: tags where at least one is required (OR). Empty = no OR clause.
    - available_only: exclude agents with current_task != null.
    - status: filter by reported status (default "online"; pass null for all).

    Returns AgentSnapshot list (with last_heartbeat_age_seconds + is_stale).
    Empty list => nobody around fits the spec; either relax requirements or queue.

    `match_agents()` with no args returns all online agents (handy roster check)."""
    matched = await _client_or_raise().match_agents(
        required_all=required_all,
        required_any=required_any,
        available_only=available_only,
        status=status,  # type: ignore[arg-type]
    )
    return _to_json([a.model_dump(mode="json") for a in matched])


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
async def update_my_model(
    model: str,
    extra_capabilities: list[str] | None = None,
) -> str:
    """Re-declare which model this agent is running. Replaces my capability tags.

    Derives `model:<exact>`, `family:<x>`, `tier:<x>`, `provider:<x>` from the
    model identifier (for known model families) and merges in any
    extra_capabilities. Use this if the runtime model has changed since the
    agent started (e.g. user switched from claude-opus-4-7 to
    claude-sonnet-4-6) so future tasks route correctly.

    Pass the exact model string (e.g. "claude-opus-4-7", "claude-sonnet-4-6",
    "gpt-4o", "gemini-2.5-pro", "llama-3.3-70b")."""
    caps = _resolve_capabilities(model, extra_capabilities)
    if not caps:
        return _to_json({"error": "empty model + extra_capabilities"})
    agent = await _client_or_raise().update_capabilities(caps)
    _client_or_raise().agent = agent
    return _to_json(agent.model_dump(mode="json"))


@mcp.tool()
@_with_pending
async def update_my_capabilities(capabilities: list[str]) -> str:
    """Replace my capability tags with the given list (full replacement, not merge).

    Prefer `update_my_model` if you just want to declare model/family/tier;
    use this when you need precise control or are adding/removing non-model
    tags (e.g. vision, code-review)."""
    agent = await _client_or_raise().update_capabilities(capabilities)
    _client_or_raise().agent = agent
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
    depends_on: list[str] | None = None,
) -> str:
    """Create a task. assigned_to=null leaves it open. priority: low, normal, high, urgent.

    Capability routing: required_all = tags every claimer must have (AND).
    required_any = at least one of these tags required (OR). Both default to empty
    (any agent can claim). Common tags: model:<exact>, family:claude, tier:opus,
    provider:anthropic, plus free-form flags like vision, audio.

    depends_on: list of upstream task IDs that must complete before this task
    is dispatchable. Lets you fire a multi-step plan in one go — broker holds
    each downstream task as `blocked` until its full dependency set is
    `completed`, then auto-emits task_event:created. If any upstream fails,
    the entire downstream subtree cascades to `failed` with a note.
    Workers fetch upstream results via mcp__mab__list_tasks / get_task_info
    when their description references the upstream IDs."""
    task = await _client_or_raise().create_task(
        title=title,
        description=description,
        assigned_to=assigned_to,
        priority=priority,  # type: ignore[arg-type]
        required_all=required_all,
        required_any=required_any,
        depends_on=depends_on,
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
async def wait_for_task(timeout_seconds: int = 60) -> str:
    """Block until an actionable task event arrives via WS push, or timeout.

    Push-driven primitive for worker-mode loops. Replaces polling with Monitor
    sleeps — the broker pushes task_event:created (and other events) over the
    WebSocket as soon as they happen; this tool blocks on the local event
    queue and returns within ~100ms of arrival.

    Returns:
      - The next actionable task as JSON: a task with status="pending" (any
        agent matching caps can claim) OR status="assigned" + assigned_to=me.
      - {"timeout": true} if no actionable event in the window.

    Non-actionable echoes (own claim/complete events, tasks assigned to other
    agents) are silently dropped — workers don't see noise. Default timeout
    60s — short enough to let the LLM loop re-evaluate state regularly,
    long enough to amortise tool-call overhead."""
    client = _client_or_raise()
    my_id = client.agent.id if client.agent else None
    task = await client.pop_one_task_event(
        float(timeout_seconds), actionable_for_id=my_id
    )
    if task is None:
        return _to_json({"timeout": True})
    return _to_json(task.model_dump(mode="json"))


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


def _resolve_capabilities(
    model: str | None,
    extra_capabilities: list[str] | None,
) -> list[str]:
    caps: list[str] = []
    if model:
        caps.extend(derive_capabilities_from_model(model))
    if extra_capabilities:
        caps.extend(extra_capabilities)
    seen: set[str] = set()
    return [c for c in caps if not (c in seen or seen.add(c))]


async def _serve(
    broker_url: str,
    api_key: str,
    *,
    model: str | None = None,
    extra_capabilities: list[str] | None = None,
) -> None:
    global _client
    _client = BrokerClient(broker_url=broker_url, api_key=api_key)

    # PATCH capabilities BEFORE WS connect so the broker's online broadcast
    # (and any concurrent task creates) see the fresh tag set, not whatever
    # gen-key left behind.
    caps = _resolve_capabilities(model, extra_capabilities)
    if caps:
        updated = await _client.update_capabilities(caps)
        log.info("capabilities pre-declared: %s", caps)
        _client.agent = updated

    await _client.start()

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
