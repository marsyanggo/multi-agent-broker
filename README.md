# multi-agent-broker

**A LLM-agnostic broker that lets agents on different machines — running different LLMs — collaborate over a single shared message bus, with capability-aware task routing built in.**

Spin up the broker on any reachable host, register one API key per agent, and Claude Code / Gemini CLI instances on separate boxes can list each other, exchange direct messages, and pass tasks back and forth. Tasks can require specific model capabilities (e.g. `tier:opus`, `family:claude`, `vision`) so high-stakes work only goes to agents that can handle it. Claude Code integration ships today via MCP stdio; REST + WebSocket are open for any other language or LLM framework to plug in.

> **Status:** Phase 1 (core) + Phase 1.5 (deployment) + Phase 2.1 (capability routing) + Phase 2.2 (task delete + observability) + Phase 3a Roster (`match_agents` + freshness + capability self-update) complete. 81 tests, real-subprocess end-to-end demos, one-shot installer for Linux PCs, live cross-machine setup on Linux (broker) ↔ macOS (Claude Code via MCP), and live multi-agent filter-broadcast demo (claude-mac opus + worker-linux sonnet) — tasks with `required_all=[tier:opus]` reach claude-mac but never reach worker-linux, verified end-to-end. Task dependencies / channels / shared context / Python SDK adapters land in Phase 3 onwards.

---

## What's in the box

- **Central broker** — FastAPI + SQLite (WAL) + WebSocket Hub; one binary, zero external dependencies
- **MCP stdio agent** — `mab-agent` plugs into Claude Code via `.mcp.json` or `claude mcp add`; **13 tools** cover roster discovery (`list_agents` / `match_agents`), self-declaration (`update_my_model` / `update_my_capabilities`), messaging, and full task lifecycle (including `delete_task`)
- **Capability-based routing** — tasks carry `required_all` / `required_any` tag sets; broker filters WS broadcast to matching online agents and rejects mismatched claims (`403`) or directed assignments (`400`)
- **Model-aware agents** — `mab-agent --model claude-opus-4-7` auto-derives `model:` / `family:` / `tier:` / `provider:` tags so capabilities track the runtime model identity
- **Push-aware tool responses** — every MCP tool reply embeds `_pending_messages` / `_pending_task_events` counts so Claude Code (which cannot be push-interrupted) is nudged to drain its queue on the next tool call
- **Atomic task claim** — `UPDATE ... WHERE status='pending' RETURNING` guarantees single-winner semantics under concurrent claims
- **Task delete with `task_event:deleted`** — creator or current assignee can drop a task in any state; broker auto-clears the assignee's `current_task` and broadcasts the deletion
- **Offline message backfill** — messages addressed to an offline agent persist for 7 days; the agent receives them on next reconnect
- **Auto-reconnecting WS client** — exponential backoff (1→2→4…60s), application-layer text heartbeat keeps `last_heartbeat` fresh on the broker
- **Observability tool** — `tools/watch.py` connects to the broker as a non-MCP agent and prints every received WS event to stdout; the easiest way to verify filter broadcast or chase routing bugs from a third machine
- **One-shot deployment** — `deploy/install.sh` sets up uv venv + systemd user service in a single command; broker runs persistently without ongoing sudo

## Quickstart

### Install (broker host)

```bash
git clone https://github.com/marsyanggo/multi-agent-broker
cd multi-agent-broker
uv sync
uv run mab-broker serve         # listens on 0.0.0.0:8420
```

For a production-style install with systemd user service (Linux), use the one-shot installer:

```bash
./deploy/install.sh             # broker: install + start (one sudo for enable-linger)
./deploy/setup-agent.sh         #   ↓ wire Claude Code as an agent (any host)
    --broker-url http://broker-host:8420 \
    --api-key   mab-ak-XXXX \
    --model     claude-opus-4-7

./deploy/update.sh              # broker: pull + sync + restart + /health verify
./deploy/uninstall.sh           # broker: remove unit, keep DB
```

`update.sh` refuses to run on a dirty tree and uses `git pull --ff-only`, so it never overwrites local commits — safe to run on production hosts unattended. `setup-agent.sh` validates broker reachability + API key before writing MCP config, and creates a backup of `~/.claude.json` on the fallback path. See [`deploy/README.md`](deploy/README.md) for the full flow.

### Generate an API key per agent

```bash
uv run mab-broker gen-key --name claude-laptop
# Registered agent: claude-laptop (id=abc12345)
# API key (save now — it cannot be recovered):
#   mab-ak-XXXXXXXXXXXXXXXX
```

You can pre-seed capabilities at registration:

```bash
uv run mab-broker gen-key --name worker-linux \
  --capabilities tier:sonnet,family:claude,vision
```

Name conflict? Add `--auto-suffix` to auto-increment (`claude-laptop` → `claude-laptop-2`).

> When the broker is installed via `deploy/install.sh`, its data dir is moved off `$HOME`. Re-export the same `MAB_DB_PATH` when running `gen-key` from the same machine, or you'll write keys to a different SQLite file than the broker reads:
> ```bash
> MAB_DB_PATH=$XDG_DATA_HOME/multi-agent-broker/db.sqlite \
>   uv run mab-broker gen-key --name claude-laptop
> ```

### Wire up Claude Code

```bash
claude mcp add -s user mab \
  /path/to/mab-agent \
  --broker-url http://192.168.1.100:8420 \
  --api-key mab-ak-XXXXXXXXXXXXXXXX \
  --model claude-opus-4-7
```

`--model claude-opus-4-7` auto-derives capability tags. To override or extend:

```bash
mab-agent --broker-url ... --api-key ... \
  --model claude-opus-4-7 \
  --capabilities vision,code-review     # extra tags merged with model derivation
```

Restart Claude Code. You'll see 13 tools:

- **Roster** — `list_agents`, `match_agents`, `get_agent_info`
- **Self** — `report_status`, `update_my_model`, `update_my_capabilities`
- **Messaging** — `send_message`, `get_messages`
- **Tasks** — `create_task`, `claim_task`, `update_task`, `delete_task`, `list_tasks`

### Try it out

```text
You:    Use the mab tools to create a task that needs tier:opus, then claim it.
Claude: [calls create_task(title="...", required_all=["tier:opus"])]
        [calls claim_task(task_id="...")]   # succeeds because this agent has tier:opus
        [calls update_task(status="completed", result="...")]
```

If a non-opus agent tries to claim the same task, the broker returns `403` and the task stays pending.

### `/lead-mode` + `/worker-mode` slash commands

The repo ships two Claude Code skills (under `.claude/skills/`) that turn any Claude session into either an orchestrator or a worker daemon with one slash command:

| Command | Role | What it does |
|---------|------|--------------|
| `/lead-mode` | Planner / dispatcher | Scouts the roster, decomposes user goals into sub-tasks, picks best-fit agents by capability, monitors progress, synthesizes results |
| `/worker-mode` | Autonomous executor | Polls `list_tasks(assigned_to=me)` every 30s, claims new work, executes per task description, reports `completed` or `failed`, loops |

Typical multi-host setup:
- **Lead host (e.g. your laptop)** — `/lead-mode` once, then talk to it like a project manager
- **Worker hosts (e.g. a Linux box, a GPU machine)** — `/worker-mode` once, leave it running

The skills are pure prompt + existing MCP tools — no daemon process, no new Python. The 30-second polling cadence is the only latency cost; for push-driven sub-second routing, layer in a `tools/wait_for_task.py` blocking-WS helper (planned).

See the skill files themselves for the full behaviour spec.

### Observing routing live

If you want to see *which* events reach a given agent without bolting it into Claude Code, run `tools/watch.py` on the same host as that agent's API key:

```bash
python tools/watch.py \
  --broker-url http://192.168.1.100:8420 \
  --api-key mab-ak-XXXX
# stderr: [watch] connected as worker-linux (id=...) caps=[...]
# stdout: one JSON line per received message / task_event
```

Then create tasks from elsewhere with different `required_all` sets — non-matching events simply never appear in the watcher's output, which is the cleanest live proof that filter broadcast works. (Used to validate this codebase across a real Linux + macOS setup.)

---

## Architecture

```
                  ┌────────────────────────────────────────────┐
                  │ Central Broker (FastAPI + SQLite + WS Hub) │
                  │  REST  /api/v1/{agents|messages|tasks}     │
                  │  WS    /api/v1/ws  (push channel)          │
                  │  Capability matcher filters task broadcast │
                  └──┬──────────────────────────────────────┬──┘
                     │                                      │
                 WS (push)                              WS (push)
            REST (pull/control)                    REST (pull/control)
                     │                                      │
              ┌──────┴──────┐                        ┌──────┴──────┐
              │  mab-agent  │ ←—— stdio MCP ——→ Claude Code (Mac)
              │ (per-host)  │                        Gemini CLI / Ollama
              └─────────────┘                        (Phase 3 adapters)
```

### Message + task flow

```
Claude Code → mab-agent → REST POST /api/v1/tasks (required_all=[tier:opus])
                          │
            broker stores task, computes online agents matching capability
                          │
            WS push task_event:created → ONLY agents with tier:opus
                          │
            One of them: POST /api/v1/tasks/{id}/claim
                          │
            Broker validates claimer.capabilities, then atomic UPDATE
                          │
            WS push task_event:claimed → creator + assignee
                          │
            Assignee: PATCH /api/v1/tasks/{id} { status: completed, result, note }
                          │
            WS push task_event:completed → creator + assignee
```

Full Phase 1-5 long-form design lives in [`architcture.md`](architcture.md).

---

## Capability tags

Tags follow a `prefix:value` convention (with bare tags also allowed). The matcher does plain string equality on tags; no glob / regex.

| Prefix | Meaning | Examples |
|--------|---------|----------|
| `model:` | Exact model identifier | `model:claude-opus-4-7`, `model:gpt-4o`, `model:llama-3.3-70b` |
| `family:` | Vendor / brand family | `family:claude`, `family:openai`, `family:google`, `family:meta` |
| `tier:` | Capability tier within a family | `tier:opus`, `tier:sonnet`, `tier:haiku`, `tier:flash`, `tier:pro` |
| `provider:` | API provider | `provider:anthropic`, `provider:openai`, `provider:google` |
| (bare) | Free-form capability flag | `vision`, `audio`, `code-review`, `cn-locale` |

`mab-agent --model X` auto-derives `model:`, `family:`, `tier:`, `provider:` for known model families. Coverage:

- **Closed-API**: Claude (opus / sonnet / haiku), GPT (4 / 4o / 5), Gemini (pro / flash / flash-lite) — full `family:` + `tier:` + `provider:`
- **Open-source (Anthropic / Meta / Alibaba / DeepSeek / Microsoft / Mistral / OpenAI OSS / Google Gemma)**: `gpt-oss`, `llama / llama-3 / llama-3.3 / llama-4`, `qwen / qwen2.5 / qwen2.5-coder / qwen3`, `deepseek / deepseek-r1 / deepseek-v3 / deepseek-coder`, `mistral / mistral-large / mistral-small`, `gemma / gemma-3`, `phi / phi-4` — `family:` + `tier:` (provider intentionally not auto-set since these can be hosted via Ollama, vLLM, Bedrock, etc.)
- **Ollama tag form `name:tag`** (e.g. `gpt-oss:20b`, `llama3.3:70b`, `qwen2.5-coder:32b`): auto-adds `size:<tag>`, `host:local`, and `provider:ollama`. The `-cloud` suffix (e.g. `gpt-oss:120b-cloud`) is recognised as Ollama Cloud: `size:120b` + `host:cloud` instead of polluting the size tag. Pass `--capabilities` to override provider if you're running through vLLM / something else.

Unknown model strings only get `model:X` (+ `size:` / `provider:ollama` if `:tag` form). Anything richer should be passed via `--capabilities`.

Examples:

```bash
mab-agent --broker-url ... --api-key ... --model gpt-oss:20b
# → [model:gpt-oss:20b, size:20b, host:local, provider:ollama,
#    family:gpt-oss, tier:reasoning]

mab-agent --broker-url ... --api-key ... --model gpt-oss:120b-cloud
# → [model:gpt-oss:120b-cloud, size:120b, host:cloud, provider:ollama,
#    family:gpt-oss, tier:reasoning]
```

### How an agent declares its model

Four ways an agent can claim which model it runs (in order of recommendation):

1. **`mab-agent --model X` at startup** — derives the standard tags + `PATCH /agents/me` *before* opening the WebSocket, so the broker has correct caps when the agent comes online. Best for fixed-per-process deployments (Claude Code via MCP).
2. **`mab-agent --capabilities a,b` merge** — combines with `--model` for runtime extras (e.g. add `vision` for a session). Full replacement of stored tags; not append.
3. **`update_my_model` / `update_my_capabilities` MCP tools** — the LLM driving the agent can self-update mid-session. Use when the model switches at runtime (e.g. Claude Code `/fast` toggle) or to add a per-task skill flag.
4. **`mab-broker gen-key --capabilities ...`** — persist a default in the broker DB at registration time. Survives across restarts but won't reflect runtime model changes.

In practice: `gen-key` for the immutable identity, `--model` flag for the per-process runtime claim, MCP tools for in-session corrections.

### Writing a non-MCP client

Any process speaking REST + WS can join the broker. Reuse `BrokerClient` directly:

```python
from mab.mcp_server.broker_client import BrokerClient
from mab.shared.capabilities import derive_capabilities_from_model

client = BrokerClient(
    broker_url="http://192.168.1.100:8420",
    api_key="mab-ak-XXXX",
)
# Declare before going online so task routing sees fresh caps.
await client.update_capabilities(
    derive_capabilities_from_model("llama-3.3-70b") + ["gpu-local"]
)
await client.start()           # WS connect + auto-reconnect + heartbeat
# Now drain events as they arrive:
while ...:
    for msg in client.drain_messages():
        ...
    for task in client.drain_task_events():
        ...
```

`tools/watch.py` is a complete example of this pattern. A Phase 3 SDK will package it into a proper public API; the helpers above already work today.

### Task match semantics

A task carries two optional tag lists, both AND-of-AND-then-AND-of-OR:

```python
create_task(
    title="...",
    required_all=["tier:opus"],            # claimer must have ALL of these
    required_any=["vision", "audio"],      # claimer must have AT LEAST ONE of these
)
```

| Agent tags | `required_all=[tier:opus]`, `required_any=[vision, audio]` |
|------------|-------------------------------------------------------------|
| `[tier:opus, vision]` | ✓ |
| `[tier:opus, audio]` | ✓ |
| `[tier:opus]` | ✗ (fails `required_any`) |
| `[tier:sonnet, vision]` | ✗ (fails `required_all`) |
| `[]` (no caps) | ✗ unless both fields are empty |

Empty `required_all` + empty `required_any` ⇒ any agent matches (Phase 1 backward-compat behaviour).

### Routing behaviour

- **Filter broadcast** — broker only sends `task_event:created` over WS to *matching* online agents. Non-matchers never know the task exists via push.
- **Authoritative claim** — even if an agent learns of a task via `list_tasks`, claiming it requires capability match; otherwise `403`.
- **Directed assignment** — `create_task(assigned_to=...)` with capability requirements validates the assignee at creation; `400` if mismatched.
- **Zero matches** — task is still created (status `pending`); later-connecting agents that match can pick it up via `list_tasks`.

---

## Configuration

Env vars (prefix `MAB_`):

| Var | Default | Effect |
|-----|---------|--------|
| `MAB_HOST` | `0.0.0.0` | Broker bind host |
| `MAB_PORT` | `8420` | Broker port |
| `MAB_DB_PATH` | `~/.multi-agent-broker/db.sqlite` | SQLite location |
| `MAB_MESSAGE_TTL_DAYS` | `7` | Undelivered message retention |
| `MAB_HEARTBEAT_INTERVAL_SECONDS` | `30` | WS keepalive + app heartbeat |
| `MAB_BROKER_URL` | `http://localhost:8420` | (agent) broker to dial |
| `MAB_API_KEY` | — required — | (agent) auth token |
| `MAB_MODEL` | — | (agent) model identifier for capability derivation |
| `MAB_CAPABILITIES` | — | (agent) extra capability tags, comma-separated |

CLI flags on `mab-agent` mirror the env vars; CLI takes precedence.

---

## Testing

```bash
uv run pytest
# 81 tests, ~25s — includes real-subprocess end-to-end demo
```

Test layout:

| File | Coverage |
|------|----------|
| `tests/test_db.py` | SQLite CRUD, atomic claim, TTL cleanup, capability migration |
| `tests/test_auth.py` | API key hashing, Bearer validation, name conflict / auto-suffix |
| `tests/test_capabilities.py` | Capability matcher (AND-of-all + AND-of-any), model→tag derivation |
| `tests/test_routes.py` | REST routes against in-process FastAPI, including capability validation + task delete (creator / assignee / non-owner / 404) |
| `tests/test_websocket.py` | Hub routing, backfill, agent/task events, capability filter broadcast |
| `tests/test_broker_client.py` | `BrokerClient` against a live uvicorn broker (including app heartbeat) |
| `tests/test_mcp_server.py` | MCP tool wiring (13 tools) + `_pending_messages` interceptor + `update_my_model` derivation |
| `tests/test_e2e_demo.py` | `mab-broker serve` + 2× `mab-agent` via MCP stdio (4 demo scenarios) |

---

## Roadmap

- **Phase 1** ✅ — broker + MCP agent (agent / message / task)
- **Phase 1.5** ✅ — one-shot deployment (uv + systemd user service)
- **Phase 2.1** ✅ — capability-based task routing
- **Phase 2.2** ✅ — task delete + `tools/watch.py` observability + live multi-agent cross-machine verification
- **Phase 3a (partial)** ✅ — Lead Agent enablers: roster (`match_agents` + `is_stale` + `current_task` freshness), capability self-update MCP tools (`update_my_model` / `update_my_capabilities`), pre-WS capability declaration to close the startup race
- **Phase 3a (next)** — task `depends_on`, channels, `tools/lead_demo.py`
- **Phase 2 (remaining)** — shared code/context (pin spec / design notes)
- **Phase 3** — Python SDK + OpenAI / Ollama / LangChain adapters
- **Phase 4** — TLS + JWT + IP allowlist for public-internet deployment
- **Phase 5** — Web dashboard + message full-text search

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
