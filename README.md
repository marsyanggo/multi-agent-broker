# multi-agent-broker

**A LLM-agnostic broker that lets agents on different machines — running different LLMs — collaborate over a single shared message bus.**

Spin up the broker on any reachable host, register one API key per agent, and Claude Code / Gemini CLI instances on separate boxes can list each other, exchange direct messages, and pass tasks back and forth. Phase 1 ships Claude Code integration via MCP stdio; REST + WebSocket are open for any other language or LLM framework to plug in.

> **Status:** Phase 1 complete — agent + message + task primitives, MCP server tested end-to-end with two `mab-agent` subprocesses driving real broker traffic. Channels / shared context / Python SDK adapters land in Phase 2-3.

---

## What's in the box

- **Central broker** — FastAPI + SQLite (WAL) + WebSocket Hub; one binary, zero ops
- **MCP stdio agent** — `mab-agent` plugs into Claude Code via `.mcp.json`; 9 tools cover agent discovery, direct messaging, task lifecycle
- **Push-aware tool responses** — every MCP tool reply embeds `_pending_messages` / `_pending_task_events` counts so Claude Code (which cannot be push-interrupted) is nudged to drain its queue on the next tool call
- **Atomic task claim** — `UPDATE ... WHERE status='pending' RETURNING` guarantees single-winner semantics under concurrent claims
- **Offline message backfill** — messages addressed to an offline agent persist for 7 days; the agent receives them on next reconnect
- **Auto-reconnecting WS client** — exponential backoff (1→2→4…60s), self-heals across broker restarts

## Quickstart

### Install

```bash
git clone <repo>
cd multi-agent-broker
uv sync
```

### 1. Start the broker (one machine)

```bash
uv run mab-broker serve
# → listens on 0.0.0.0:8420
```

### 2. Generate an API key per agent

```bash
uv run mab-broker gen-key --name claude-laptop
# Registered agent: claude-laptop (id=abc12345)
# API key (save now — it cannot be recovered):
#   mab-ak-XXXXXXXXXXXXXXXX
```

Name conflict? Add `--auto-suffix` to auto-increment (`claude-laptop` → `claude-laptop-2`).

### 3. Wire up Claude Code

Add to the agent machine's `.mcp.json`:

```json
{
  "mcpServers": {
    "mab": {
      "command": "uv",
      "args": ["run", "mab-agent",
               "--broker-url", "http://192.168.1.100:8420",
               "--api-key", "mab-ak-XXXXXXXXXXXXXXXX"]
    }
  }
}
```

Restart Claude Code. You'll see 9 tools: `list_agents`, `get_agent_info`, `report_status`, `send_message`, `get_messages`, `create_task`, `claim_task`, `update_task`, `list_tasks`.

### 4. Test it

In one Claude Code session: ask it to call `create_task(title="say hi back")`. In another session: ask it to `list_tasks(status="pending")` → `claim_task` → `update_task(status="completed")`. The two sides should see each other's events propagated via WebSocket within sub-second latency.

---

## Architecture

```
┌────────────────────────────────────────────┐
│ Central Broker (FastAPI + SQLite + WS Hub) │
│  REST  /api/v1/{agents|messages|tasks}     │
│  WS    /api/v1/ws  (push channel)          │
└──────┬─────────────────────────────┬───────┘
       │                             │
   WS (push)                    REST (pull)
       │                             │
┌──────┴───────┐               ┌─────┴────────┐
│  mab-agent   │ ← stdio ──→  │ Claude Code  │
│  (per-host)  │   MCP         │ (per-host)   │
└──────────────┘               └──────────────┘
```

Full Phase 1-5 design lives in [`architcture.md`](architcture.md).

---

## Configuration

Env vars (prefix `MAB_`):

| Var | Default | Effect |
|---|---|---|
| `MAB_HOST` | `0.0.0.0` | Broker bind host |
| `MAB_PORT` | `8420` | Broker port |
| `MAB_DB_PATH` | `~/.multi-agent-broker/db.sqlite` | SQLite location |
| `MAB_MESSAGE_TTL_DAYS` | `7` | Undelivered message retention |
| `MAB_HEARTBEAT_INTERVAL_SECONDS` | `30` | WS keepalive |
| `MAB_BROKER_URL` | `http://localhost:8420` | (agent side) broker to dial |
| `MAB_API_KEY` | — required — | (agent side) auth token |

---

## Testing

```bash
uv run pytest
# 51 tests, ~25s — includes real-subprocess end-to-end demo
```

Test layout:
- `tests/test_db.py` — SQLite CRUD + atomic claim + TTL
- `tests/test_auth.py` — API key hashing + Bearer validation
- `tests/test_routes.py` — REST routes against in-process FastAPI
- `tests/test_websocket.py` — Hub routing, backfill, agent/task events
- `tests/test_broker_client.py` — `BrokerClient` against live uvicorn
- `tests/test_mcp_server.py` — MCP tool wiring + pending-count interceptor
- `tests/test_e2e_demo.py` — `mab-broker serve` + 2× `mab-agent` via MCP stdio

---

## Roadmap

- **Phase 1** ✅ — broker + MCP agent (agent / message / task)
- **Phase 2** — channels + shared code/context + broadcast
- **Phase 3** — Python SDK + OpenAI / Ollama / LangChain adapters
- **Phase 4** — TLS + JWT + IP allowlist for public-internet deployment
- **Phase 5** — Web dashboard + message full-text search

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
