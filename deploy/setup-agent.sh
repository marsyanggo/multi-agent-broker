#!/usr/bin/env bash
set -euo pipefail

# One-shot agent wiring for multi-agent-broker.
#
# Given a broker URL + an API key, register this host's Claude Code as an
# `mab` MCP server. Safe to re-run; idempotent.
#
# Two callers:
#   - From the broker host: pair this with `gen-key` to make the broker box
#     act as an agent itself.
#   - From a remote agent host: someone already ran `gen-key` on the broker
#     and gave you the key string.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MAB_AGENT_BIN="$REPO_ROOT/.venv/bin/mab-agent"

# --- defaults / env ---
BROKER_URL="${MAB_BROKER_URL:-}"
API_KEY="${MAB_API_KEY:-}"
MODEL="${MAB_MODEL:-}"
NAME="mab"
WORKER_HOST=false

usage() {
    cat <<EOF
Usage: $0 --broker-url URL --api-key KEY [--model MODEL] [--name NAME] [--worker-host]

  --broker-url URL   broker REST endpoint (e.g. http://192.168.1.100:8420)
  --api-key KEY      mab-ak-... API key generated on the broker host
  --model MODEL      (optional) model identifier to declare on startup
                     (e.g. claude-opus-4-7, claude-sonnet-4-6, gpt-oss:120b-cloud)
  --name NAME        (optional) MCP server name in ~/.claude.json (default: mab)
  --worker-host      (optional) ALSO configure <repo>/.claude/settings.local.json
                     to bypass Claude Code permission prompts. Required for
                     /worker-mode autonomy on this host. The .local.json file
                     is gitignored, so this stays per-host. DESTRUCTIVE FOR
                     INTERACTIVE USE: don't pass this on your dev box.

Env vars MAB_BROKER_URL / MAB_API_KEY / MAB_MODEL are honoured if flags omitted.
EOF
}

# --- arg parse ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --broker-url)   BROKER_URL="$2"; shift 2 ;;
        --api-key)      API_KEY="$2";    shift 2 ;;
        --model)        MODEL="$2";      shift 2 ;;
        --name)         NAME="$2";       shift 2 ;;
        --worker-host)  WORKER_HOST=true; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) echo "error: unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$BROKER_URL" || -z "$API_KEY" ]]; then
    echo "error: --broker-url and --api-key are required" >&2
    usage >&2
    exit 2
fi

if [[ ! -x "$MAB_AGENT_BIN" ]]; then
    echo "error: mab-agent binary not found at $MAB_AGENT_BIN" >&2
    echo "       Run ./deploy/install.sh first, or uv sync from the repo root." >&2
    exit 1
fi

# --- 1. broker reachability ---
echo "==> Probing $BROKER_URL/health"
if ! curl -sf "$BROKER_URL/health" >/dev/null; then
    echo "error: $BROKER_URL/health unreachable. Is the broker running and the URL right?" >&2
    exit 1
fi
echo "    OK"

# --- 2. api key validity ---
echo "==> Verifying API key (GET /agents/me)"
HTTP_CODE="$(curl -sS -o /tmp/mab-me.json -w '%{http_code}' \
    -H "Authorization: Bearer $API_KEY" \
    "$BROKER_URL/api/v1/agents/me")"
if [[ "$HTTP_CODE" != "200" ]]; then
    echo "error: GET /agents/me returned HTTP $HTTP_CODE" >&2
    cat /tmp/mab-me.json >&2 2>/dev/null || true
    echo >&2
    echo "       The API key may be invalid, or was generated against a different broker DB." >&2
    rm -f /tmp/mab-me.json
    exit 1
fi
AGENT_NAME="$(python3 -c "import json; print(json.load(open('/tmp/mab-me.json'))['name'])" 2>/dev/null || echo "?")"
echo "    OK — key belongs to agent '$AGENT_NAME'"
rm -f /tmp/mab-me.json

# --- 3. build the mab-agent args ---
MAB_ARGS=("--broker-url" "$BROKER_URL" "--api-key" "$API_KEY")
if [[ -n "$MODEL" ]]; then
    MAB_ARGS+=("--model" "$MODEL")
fi

# --- 4. write MCP config ---
# Prefer `claude mcp add` if the CLI is on PATH (handles JSON merging correctly).
# Fall back to direct JSON edit via python.

if command -v claude >/dev/null 2>&1; then
    echo "==> Using \`claude mcp add\` to register MCP server '$NAME'"
    # `claude mcp add` will fail if the name already exists. Remove first (idempotent).
    claude mcp remove "$NAME" -s user >/dev/null 2>&1 || true
    claude mcp add -s user "$NAME" "$MAB_AGENT_BIN" -- "${MAB_ARGS[@]}"
    echo "    OK — server '$NAME' added at user scope"
else
    echo "==> \`claude\` CLI not on PATH; editing ~/.claude.json directly"
    python3 - "$NAME" "$MAB_AGENT_BIN" "${MAB_ARGS[@]}" <<'PYEOF'
import json, os, sys, shutil
from pathlib import Path

name = sys.argv[1]
command = sys.argv[2]
args = sys.argv[3:]

cfg_path = Path.home() / ".claude.json"
if cfg_path.exists():
    backup = cfg_path.with_suffix(".json.mab-bak")
    shutil.copy2(cfg_path, backup)
    print(f"    backup: {backup}")
    with cfg_path.open() as f:
        cfg = json.load(f)
else:
    cfg = {}

mcp_servers = cfg.setdefault("mcpServers", {})
mcp_servers[name] = {"command": command, "args": args}

with cfg_path.open("w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"    OK — mcpServers['{name}'] written to {cfg_path}")
PYEOF
fi

# --- 5. verify (best-effort) ---
if command -v claude >/dev/null 2>&1; then
    echo "==> Verifying via \`claude mcp list\`"
    if claude mcp list 2>/dev/null | grep -q "^${NAME}:"; then
        echo "    OK — '$NAME' present in claude config"
    else
        echo "    warn: '$NAME' not visible to \`claude mcp list\` (may need session restart)" >&2
    fi
fi

# --- 6. worker-host setup (optional) ---
if $WORKER_HOST; then
    SETTINGS_PATH="$REPO_ROOT/.claude/settings.local.json"
    echo "==> Configuring $SETTINGS_PATH for worker-mode autonomy"
    mkdir -p "$(dirname "$SETTINGS_PATH")"
    python3 - "$SETTINGS_PATH" <<'PYEOF'
import json, shutil, sys
from pathlib import Path

cfg_path = Path(sys.argv[1])
if cfg_path.exists():
    backup = cfg_path.with_suffix(".json.mab-bak")
    shutil.copy2(cfg_path, backup)
    print(f"    backup: {backup}")
    with cfg_path.open() as f:
        cfg = json.load(f)
else:
    cfg = {}

permissions = cfg.setdefault("permissions", {})
prev_mode = permissions.get("defaultMode")
permissions["defaultMode"] = "bypassPermissions"
# Preserve any existing allow / deny lists.
permissions.setdefault("allow", [])

with cfg_path.open("w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

if prev_mode == "bypassPermissions":
    print(f"    OK — permissions.defaultMode already 'bypassPermissions'")
else:
    print(f"    OK — permissions.defaultMode = 'bypassPermissions' (was: {prev_mode!r})")
PYEOF
    echo "    NOTE: This bypasses all Claude Code permission prompts for sessions"
    echo "          opened in $REPO_ROOT. Don't run /lead-mode or interactive dev"
    echo "          work in this directory on this host unless you're OK with that."
fi

# --- 7. next steps ---
WORKER_LINE=""
if $WORKER_HOST; then
    WORKER_LINE="  Worker host:      yes — permission prompts bypassed for this repo"
fi
cat <<EOF

✓ Agent MCP wiring done.

  MCP server name:  $NAME
  Broker URL:       $BROKER_URL
  Agent identity:   $AGENT_NAME
  Model declared:   ${MODEL:-(none — gen-key default will apply)}
${WORKER_LINE}

Next steps:
  1. **Restart your Claude Code session** — MCP servers only load at startup.
  2. In the new session, try one of the mab tools:
       claude (start session)  →  /worker-mode    (autonomous worker)
                              or  /lead-mode      (orchestrator)
                              or any mcp__mab__* tool to confirm wiring.
  3. If anything looks off, \`claude mcp list\` should show '$NAME: ✓ Connected'.

EOF
