# multi-agent-broker

**Make LLMs from different vendors collaborate as one team — by capability, not by name.**

A single-vendor agent stack (Claude Code subagents, OpenAI Assistants, Gemini agents) can already coordinate N copies of *its* model. mab-broker is for the harder problem: **the right LLM for this sub-task lives in another vendor's stack, on your own hardware, or split across both**. Examples this codebase exists to enable:

- **Claude Opus** plans a workflow, dispatches the heavy reasoning step to **gpt-oss:120b on Ollama Cloud** (1/10 the cost), then routes the polishing pass to **Claude Sonnet via subscription** — all in one autonomous task chain.
- **Local Llama-70b on your GPU box** handles PII-bearing input (no cloud), the same lead also dispatches non-sensitive sub-tasks to **GPT-4o**, both happen in the same plan.
- Anthropic gets rate-limited mid-workflow → broker reroutes the next task to **DeepSeek-R1** automatically, because capability is the contract and vendor is interchangeable.

Tasks declare **what they need** (`required_all=["tier:reasoning", "host:cloud"]`), agents declare **what they offer** (`["model:gpt-oss:120b-cloud", "family:gpt-oss", "tier:reasoning", "host:cloud", "provider:ollama"]`), and the broker does the matching. You can swap a worker's underlying LLM tomorrow without changing a single lead-side prompt.

### Where mab-broker isn't a fit

- **Single-vendor setups** — you already have great native options. Use those.
- **Public-internet multi-tenant** — TLS / wss / JWT / IP allowlist still pending (Phase 4). Today this is LAN / VPN / Tailscale.
- **Synchronous chat** — broker is task-shaped, not turn-based dialogue.

### What it looks like in practice

```
┌─────────────────────────────────────────────────────────────┐
│  Lead: Claude Opus (Anthropic API)                          │  ← your laptop
│    "Plan, dispatch, monitor, synthesize"                    │
└──────────────────────┬──────────────────────────────────────┘
              create_task(required_all=[...])
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Broker (FastAPI + SQLite + WS)                             │  ← your network
│    Capability-filtered push · atomic claim · data sovereign │
└──┬────────────────────────────────────────┬─────────────────┘
   │ tier:reasoning + host:cloud            │ tier:sonnet + family:claude
   ▼                                         ▼
┌──────────────────────┐                ┌──────────────────────┐
│  Worker A            │                │  Worker B            │
│  adapter: ollama     │                │  adapter: claude-cli │
│  model: gpt-oss:120b │                │  model: claude-sonnet│
│  via Ollama Cloud    │                │  via Max subscription│
│  (OpenAI OSS family) │                │  (Anthropic family)  │
└──────────────────────┘                └──────────────────────┘

                          + add a worker for:
                            – OpenAI direct (API key)
                            – Gemini direct (API key)
                            – Local Llama / Qwen / DeepSeek (GPU box, no internet)
                            – any HTTP-speaking LLM (one ~50-line adapter)
```

One Python daemon (`mab-worker`) per worker host, four built-in adapters (`anthropic` / `ollama` / `claude-cli` / `mock`), swap with one CLI flag — `--adapter X --model Y`. **No client-side change for the lead when you swap vendors.**

> **Status:** Phase 1 (core) + Phase 1.5 (deployment) + Phase 2.1 (capability routing) + Phase 2.2 (task delete + observability) + Phase 3a Roster + **Phase 3 fully complete** — worker daemon SDK + `depends_on` task chains + shared context + channels. 188 tests, real-subprocess end-to-end demos, one-shot installers for both broker and worker hosts, live cross-machine multi-LLM proof: claude-mac (Opus via Anthropic API) as lead orchestrates worker-gpt-oss-cloud (gpt-oss:120b via Ollama Cloud) running as a headless `mab-worker` systemd daemon — push-driven task routing settles in ~1 second end-to-end (broker push + daemon claim + Ollama inference + result write-back). End-to-end natural-language demo (`/lead-mode` builds a playable Chrome-dino game across two vendor workers in ~5 minutes) verified — see [`docs/cookbook.md`](docs/cookbook.md) Recipe 6 + [`examples/dino.html`](examples/dino.html).

---

## What's in the box

Three runtime roles, one repo, one broker URL:

- **Central broker** (`mab-broker`) — FastAPI + SQLite (WAL) + WebSocket Hub; one binary, zero external dependencies
- **Interactive MCP agent** (`mab-agent`) — plugs into Claude Code via `.mcp.json` or `claude mcp add`; **14 tools** cover roster discovery (`list_agents` / `match_agents`), self-declaration (`update_my_model` / `update_my_capabilities`), messaging, full task lifecycle (including `delete_task`), and push-driven `wait_for_task`. Best for the **lead** orchestrator and any human-in-the-loop dev work
- **Autonomous worker daemon** (`mab-worker`) — headless Python daemon supervised by `systemd --user`; pulls tasks via push-driven `wait_for_task`, dispatches each through a pluggable LLM adapter, reports back. **Four adapters** ship in-box: `anthropic` (api.anthropic.com direct via httpx), `ollama` (works for local Ollama and Ollama Cloud, also via httpx), `claude-cli` (spawns `claude -p` per task — gives the worker access to Claude Code's full tool ecosystem), and `mock` (in-process echo for tests). Best for **production workers** — daemon mode avoids the Claude Code TUI's user-input-breaks-the-loop race and is supervised, immortal, and observable via journalctl

Plus the cross-cutting machinery:

- **Capability-based routing** — tasks carry `required_all` / `required_any` tag sets; broker filters WS broadcast to matching online agents and rejects mismatched claims (`403`) or directed assignments (`400`)
- **Model-aware agents** — `--model claude-opus-4-7` auto-derives `model:` / `family:` / `tier:` / `provider:` tags so capabilities track the runtime model identity (works on both `mab-agent` and `mab-worker`)
- **Push-aware tool responses** — every MCP tool reply embeds `_pending_messages` / `_pending_task_events` counts so Claude Code (which cannot be push-interrupted) is nudged to drain its queue on the next tool call
- **Atomic task claim** — `UPDATE ... WHERE status='pending' RETURNING` guarantees single-winner semantics under concurrent claims
- **Task delete with `task_event:deleted`** — creator or current assignee can drop a task in any state; broker auto-clears the assignee's `current_task` and broadcasts the deletion
- **Offline message backfill** — messages addressed to an offline agent persist for 7 days; the agent receives them on next reconnect
- **Auto-reconnecting WS client** — exponential backoff (1→2→4…60s), application-layer text heartbeat keeps `last_heartbeat` fresh on the broker
- **Observability tool** — `tools/watch.py` connects to the broker as a non-MCP agent and prints every received WS event to stdout; the easiest way to verify filter broadcast or chase routing bugs from a third machine
- **One-shot deployment** — `deploy/install.sh` (broker), `deploy/setup-agent.sh` (Claude Code MCP wiring), `deploy/setup-worker.sh` (daemon) — each is idempotent; `deploy/update.sh` does git pull + uv sync + service restart in one go

## Quickstart

### Install (broker host)

```bash
git clone https://github.com/marsyanggo/multi-agent-broker
cd multi-agent-broker
uv sync
uv run mab-broker serve         # listens on 0.0.0.0:8420
```

For a production-style install with systemd user services (Linux), use the one-shot installers:

```bash
# Broker host
./deploy/install.sh                                          # one sudo for enable-linger; rest non-sudo

# Interactive Claude Code agent (any host — lead, dev, debug)
./deploy/setup-agent.sh \
    --broker-url http://broker-host:8420 \
    --api-key   mab-ak-XXXX \
    --model     claude-opus-4-7

# Headless worker daemon (any host — production task processor)
./deploy/setup-worker.sh \
    --broker-url http://broker-host:8420 \
    --api-key   mab-ak-YYYY \
    --adapter   ollama \
    --model     gpt-oss:120b-cloud \
    --ollama-base-url http://localhost:11434           # via local Ollama proxy

# Update + uninstall
./deploy/update.sh                                           # any host: pull + sync + restart + verify
./deploy/uninstall.sh                                        # broker host: remove unit, keep DB
```

`update.sh` refuses to run on a dirty tree and uses `git pull --ff-only`, so it never overwrites local commits — safe to run on production hosts unattended. `setup-agent.sh` validates broker reachability + API key before writing MCP config, and creates a backup of `~/.claude.json` on the fallback path. `setup-worker.sh` writes a `chmod 600` systemd unit with all config (including secrets) as `Environment=` lines — `--name <suffix>` lets one host run several workers in parallel (`mab-worker-opus.service` + `mab-worker-llama.service`, etc.). See [`deploy/README.md`](deploy/README.md) for the full flow.

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

Restart Claude Code. You'll see 14 tools:

- **Roster** — `list_agents`, `match_agents`, `get_agent_info`
- **Self** — `report_status`, `update_my_model`, `update_my_capabilities`
- **Messaging** — `send_message`, `get_messages`
- **Tasks** — `create_task`, `claim_task`, `update_task`, `delete_task`, `list_tasks`
- **Worker** — `wait_for_task` (push-driven block on broker WS, used by /worker-mode)

### Install a worker daemon (production)

For headless task execution without a Claude Code session in the loop, install `mab-worker` as a systemd user service:

```bash
# On the worker host (broker generated the key, then handed it to you)
./deploy/setup-worker.sh \
  --broker-url http://broker-host:8420 \
  --api-key   mab-ak-YYYYYYYYYYYYYY \
  --adapter   ollama \
  --model     gpt-oss:120b-cloud \
  --ollama-base-url http://localhost:11434       # local Ollama proxies to Ollama Cloud
```

Adapter choices (`--adapter`):

| Adapter | Backend | Best for |
|---------|---------|----------|
| `anthropic` | POST to `api.anthropic.com/v1/messages` direct via httpx | Pure-LLM tasks against the official Claude API |
| `ollama` | POST to `/api/chat` direct via httpx; auto-handles local + Ollama Cloud auth | Local GPU models or Ollama Cloud-routed models like `gpt-oss:120b-cloud` |
| `claude-cli` | Spawns `claude -p <prompt>` per task | Tasks needing Claude Code's tool ecosystem (Bash / Edit / Read / web). Heaviest cold start but most capable |
| `mock` | In-process echo | Smoke tests, dry runs |

After install, the daemon is supervised:

```bash
systemctl --user status mab-worker.service        # is it up?
journalctl --user -u mab-worker.service -f        # tail logs
systemctl --user restart mab-worker.service       # apply config / rotate key
./deploy/update.sh && systemctl --user restart mab-worker.service   # pull repo + restart
```

Run multiple workers on one host with `--name <suffix>` — e.g. `--name opus-cloud` creates `mab-worker-opus-cloud.service` alongside the default `mab-worker.service`, each with its own unit file, secrets, and journal.

### Try it out

```text
You:    Use the mab tools to create a task that needs tier:opus, then claim it.
Claude: [calls create_task(title="...", required_all=["tier:opus"])]
        [calls claim_task(task_id="...")]   # succeeds because this agent has tier:opus
        [calls update_task(status="completed", result="...")]
```

If a non-opus agent tries to claim the same task, the broker returns `403` and the task stays pending.

### `/lead-mode` + `/worker-mode` slash commands

The repo ships two Claude Code skills (under `.claude/skills/`) that turn any Claude session into either an orchestrator or a worker with one slash command:

| Command | Role | What it does |
|---------|------|--------------|
| `/lead-mode` | Planner / dispatcher | Scouts the roster, decomposes user goals into sub-tasks, picks best-fit agents by capability, monitors progress, synthesizes results |
| `/worker-mode` | Interactive worker | Blocks on `wait_for_task` (push-driven, sub-second latency); claims new work the instant the broker pushes it, executes per task description, reports `completed` or `failed`, loops. **For production workers, prefer `mab-worker` daemon** — `/worker-mode` is fine for dev / debug, but Claude Code's interactive TUI means a stray user keystroke breaks the loop. The daemon has no TUI and is supervised by systemd. |

Typical multi-host setup:
- **Lead host (laptop)** — `/lead-mode` once in Claude Code, then talk to it like a project manager
- **Production worker hosts (GPU / cloud-LLM boxes)** — `setup-worker.sh` once, daemon runs forever in background

The skills are pure prompt + existing MCP tools — no daemon process, no new Python. Worker mode runs push-driven via the `wait_for_task` MCP tool (blocks on mab-agent's WS event queue, returns within ~100ms of broker push); no polling, sub-second routing latency. The standalone `mab-worker` daemon uses the same primitive but bypasses Claude Code entirely — see [`deploy/README.md`](deploy/README.md).

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
                  └──┬─────────────────────┬──────────────────┘
                     │                     │
              WS push + REST        WS push + REST
                     │                     │
            ┌────────┴────────┐    ┌───────┴───────────┐
            │   mab-agent     │    │  mab-worker       │
            │   (MCP stdio)   │    │  (systemd daemon) │
            │                 │    │                   │
            │ Claude Code     │    │  Adapter:         │
            │ /lead-mode      │    │   anthropic       │
            │ /worker-mode    │    │   ollama          │
            │ (interactive)   │    │   claude-cli      │
            └─────────────────┘    └───────────────────┘
              lead / dev box           production worker
```

Roles compose. A small setup has the broker, one lead `mab-agent` on the user's laptop, and one `mab-worker` daemon on each GPU / model host. The broker is the only piece that needs to be reachable from every other; agents and workers are clients.

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
# 188 tests, ~35s — includes real-subprocess end-to-end demo + live-broker daemon integration
```

Test layout:

| File | Coverage |
|------|----------|
| `tests/test_db.py` | SQLite CRUD, atomic claim, TTL cleanup, capability migration |
| `tests/test_auth.py` | API key hashing, Bearer validation, name conflict / auto-suffix |
| `tests/test_capabilities.py` | Capability matcher (AND-of-all + AND-of-any), model→tag derivation (Claude / GPT / Gemini / Llama / Qwen / DeepSeek / gpt-oss / Phi + Ollama tag heuristic) |
| `tests/test_routes.py` | REST routes against in-process FastAPI, including capability validation + task delete (creator / assignee / non-owner / 404) |
| `tests/test_websocket.py` | Hub routing, backfill, agent/task events, capability filter broadcast |
| `tests/test_broker_client.py` | `BrokerClient` against a live uvicorn broker (including app heartbeat) |
| `tests/test_mcp_server.py` | MCP tool wiring (14 tools) + `_pending_messages` interceptor + `update_my_model` derivation + `wait_for_task` push/timeout/filter |
| `tests/test_e2e_demo.py` | `mab-broker serve` + 2× `mab-agent` via MCP stdio (4 demo scenarios) |
| `tests/worker/test_mock_adapter.py` | MockAdapter contract: fixed / callable / async / error / delay / lifecycle |
| `tests/worker/test_daemon.py` | WorkerDaemon against live broker: directly-assigned tasks, open-pool claim, adapter error path, per-task timeout, multi-task survival, capability declaration, setup/teardown, stats |
| `tests/worker/test_anthropic_adapter.py` | AnthropicAdapter against `httpx.MockTransport`: happy path, system prompt + temperature, multi-block concat, non-text-block skip, HTTP error mapping, empty response, missing key |
| `tests/worker/test_ollama_adapter.py` | OllamaAdapter against `httpx.MockTransport`: local no-auth, cloud bearer, system prompt, response trim, HTTP error, malformed response |
| `tests/worker/test_claude_cli_adapter.py` | ClaudeCLIAdapter with a temp Python shim mimicking `claude` CLI: version probe, model + flag wiring, prompt template, exit code mapping, empty stdout, subprocess kill on cancel, extra args |
| `tests/worker/test_cli.py` | `mab-worker` CLI: parse_capabilities, build_adapter for each of 4 adapters, env-var defaults, required-flag validation |
| `tests/worker/test_e2e_daemon.py` | End-to-end daemon scenarios: capability-rejected claim, push-after-catchup (true push path), graceful stop mid-task, 10-task burst |

---

## Roadmap

- **Phase 1** ✅ — broker + MCP agent (agent / message / task)
- **Phase 1.5** ✅ — one-shot deployment (uv + systemd user service)
- **Phase 2.1** ✅ — capability-based task routing
- **Phase 2.2** ✅ — task delete + `tools/watch.py` observability + live multi-agent cross-machine verification
- **Phase 3a** ✅ — Lead Agent enablers: roster (`match_agents` + `is_stale` + `current_task` freshness), capability self-update MCP tools (`update_my_model` / `update_my_capabilities`), pre-WS capability declaration, push-driven `wait_for_task` MCP tool
- **Phase 3** ✅ — `mab-worker` daemon SDK + 4 adapters (Anthropic / Ollama / Claude CLI / Mock), `setup-worker.sh` one-shot install, push-driven event-name-filtered task queue. Production-verified: claude-mac (Opus) → broker → daemon (gpt-oss:120b via Ollama Cloud) end-to-end in ~1s
- **Phase 3 (D — depends_on)** ✅ — task dependencies: blocked status + auto-unblock on upstream completion + failure cascade through downstream chains. Lets a lead fire a whole multi-step plan in one go instead of polling between steps.
- **Phase 3 (S — shared context)** ✅ — pinned named documents any agent can read. Solves "every task description duplicates the same style guide" and supports auto-promoting upstream task results as named handoff docs.
- **Phase 3 (CH — channels)** ✅ — named group-broadcast topics. Members get WS push for every new message; non-members can still read history via REST but don't receive pushes and can't post. Distinct from direct messages (1:1) and tasks (claim-lifecycle) — for coordination noise, status updates, open queries. 12 new tests (9 REST + 2 WS + 1 MCP).
- **Phase 3 (cookbook)** ✅ — 6 production-verified cross-vendor recipes in [`docs/cookbook.md`](docs/cookbook.md): two-stage thinking, fan-in synthesis, failure cascade, pinned project spec, group broadcast, **and `/lead-mode` end-to-end** (natural language → fan-out + fan-in plan → playable [`examples/dino.html`](examples/dino.html) artifact, ~5 min wall-clock).
- Test total: 188.
- **Phase 4** — TLS + JWT + IP allowlist for public-internet deployment
- **Phase 5** — Web dashboard + message full-text search

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
