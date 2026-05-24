#!/usr/bin/env bash
set -euo pipefail

# One-shot setup for a mab-worker daemon on this host:
#   - uv sync deps
#   - probe broker /health + validate API key against /agents/me
#   - write systemd --user unit with all config + secrets via Environment=
#   - enable linger so the service survives logout (one sudo)
#   - daemon-reload + enable/restart
#   - poll /agents/me until status=online (or warn after 10s)
#
# Idempotent — re-run to apply new config. The unit file is rewritten each
# time, then the service restarted.
#
# --name <suffix> supports multiple workers on the same host. The unit name
# becomes mab-worker-<suffix>.service (or just mab-worker.service when no
# suffix is given).

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYPROJECT="$REPO_ROOT/pyproject.toml"

if [[ ! -f "$PYPROJECT" ]] || ! grep -q "multi-agent-broker" "$PYPROJECT"; then
    echo "error: run from a multi-agent-broker checkout (no pyproject.toml at $REPO_ROOT)" >&2
    exit 1
fi

# --- defaults / env ---
BROKER_URL="${MAB_BROKER_URL:-}"
API_KEY="${MAB_API_KEY:-}"
ADAPTER="${MAB_ADAPTER:-}"
MODEL="${MAB_MODEL:-}"
CAPABILITIES="${MAB_CAPABILITIES:-}"
NAME=""
ANTHROPIC_API_KEY_V="${ANTHROPIC_API_KEY:-}"
ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
OLLAMA_API_KEY_V="${OLLAMA_API_KEY:-}"
GEMINI_API_KEY_V="${GEMINI_API_KEY:-}"
GEMINI_BASE_URL="${GEMINI_BASE_URL:-https://generativelanguage.googleapis.com}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
SYSTEM_PROMPT="${MAB_SYSTEM_PROMPT:-}"
PER_TASK_TIMEOUT="${MAB_PER_TASK_TIMEOUT:-600}"
WAIT_FOR_TASK_TIMEOUT="${MAB_WAIT_FOR_TASK_TIMEOUT:-60}"
NO_SKIP_PERMISSIONS=false

usage() {
    cat <<EOF
Usage: $0 [options]

Required:
  --broker-url URL          broker REST endpoint
  --api-key KEY             mab-ak-... API key for this worker's agent
  --adapter NAME            one of: anthropic, ollama, claude-cli, gemini, mock
  --model MODEL             model identifier (e.g. claude-sonnet-4-6,
                            gpt-oss:120b-cloud, gemini-2.5-flash, llama3.3:70b)

Optional (general):
  --name SUFFIX             unit name: mab-worker[-SUFFIX].service
                            Allows multiple workers per host. Default: none
                            (creates mab-worker.service)
  --capabilities CSV        extra capability tags beyond --model derivation
  --system-prompt TEXT      system prompt prepended to each task
                            (anthropic / ollama / gemini adapters)
  --per-task-timeout SEC    hard timeout per task (default 600)
  --wait-for-task-timeout SEC  wait_for_task block timeout (default 60)

Anthropic adapter:
  --anthropic-api-key KEY   (or \$ANTHROPIC_API_KEY)
  --anthropic-base-url URL  default https://api.anthropic.com

Ollama adapter:
  --ollama-base-url URL     default http://localhost:11434
                            (use https://ollama.com for Ollama Cloud)
  --ollama-api-key KEY      for Ollama Cloud (or \$OLLAMA_API_KEY)

Gemini adapter:
  --gemini-api-key KEY      Google AI Studio key (or \$GEMINI_API_KEY)
  --gemini-base-url URL     default https://generativelanguage.googleapis.com

Claude CLI adapter:
  --claude-bin PATH         default \$CLAUDE_BIN or "claude"
  --no-skip-permissions     don't pass --dangerously-skip-permissions

Env vars are honoured as defaults for the matching flag. Secrets in the
unit file are at \$HOME/.config/systemd/user/<unit>.service (chmod 600).
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --broker-url)            BROKER_URL="$2"; shift 2 ;;
        --api-key)               API_KEY="$2"; shift 2 ;;
        --adapter)               ADAPTER="$2"; shift 2 ;;
        --model)                 MODEL="$2"; shift 2 ;;
        --capabilities)          CAPABILITIES="$2"; shift 2 ;;
        --name)                  NAME="$2"; shift 2 ;;
        --anthropic-api-key)     ANTHROPIC_API_KEY_V="$2"; shift 2 ;;
        --anthropic-base-url)    ANTHROPIC_BASE_URL="$2"; shift 2 ;;
        --ollama-base-url)       OLLAMA_BASE_URL="$2"; shift 2 ;;
        --ollama-api-key)        OLLAMA_API_KEY_V="$2"; shift 2 ;;
        --gemini-api-key)        GEMINI_API_KEY_V="$2"; shift 2 ;;
        --gemini-base-url)       GEMINI_BASE_URL="$2"; shift 2 ;;
        --claude-bin)            CLAUDE_BIN="$2"; shift 2 ;;
        --system-prompt)         SYSTEM_PROMPT="$2"; shift 2 ;;
        --per-task-timeout)      PER_TASK_TIMEOUT="$2"; shift 2 ;;
        --wait-for-task-timeout) WAIT_FOR_TASK_TIMEOUT="$2"; shift 2 ;;
        --no-skip-permissions)   NO_SKIP_PERMISSIONS=true; shift ;;
        -h|--help)               usage; exit 0 ;;
        *) echo "error: unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# --- validate ---
errs=()
[[ -z "$BROKER_URL" ]] && errs+=("--broker-url required")
[[ -z "$API_KEY" ]] && errs+=("--api-key required")
[[ -z "$ADAPTER" ]] && errs+=("--adapter required")
[[ -z "$MODEL" ]] && errs+=("--model required")
case "$ADAPTER" in
    anthropic|ollama|claude-cli|gemini|mock) ;;
    *) errs+=("--adapter must be one of: anthropic, ollama, claude-cli, gemini, mock (got '$ADAPTER')") ;;
esac
if [[ "$ADAPTER" == "anthropic" && -z "$ANTHROPIC_API_KEY_V" ]]; then
    errs+=("anthropic adapter needs --anthropic-api-key or ANTHROPIC_API_KEY")
fi
if [[ "$ADAPTER" == "gemini" && -z "$GEMINI_API_KEY_V" ]]; then
    errs+=("gemini adapter needs --gemini-api-key or GEMINI_API_KEY")
fi
if [[ ${#errs[@]} -gt 0 ]]; then
    for e in "${errs[@]}"; do echo "error: $e" >&2; done
    exit 2
fi

# --- compute unit name ---
if [[ -z "$NAME" ]]; then
    UNIT_BASENAME="mab-worker"
else
    UNIT_BASENAME="mab-worker-${NAME}"
fi
UNIT_NAME="${UNIT_BASENAME}.service"
UNIT_PATH="$HOME/.config/systemd/user/$UNIT_NAME"

cat <<EOF
==> Worker setup
    name:       $UNIT_BASENAME
    broker:     $BROKER_URL
    adapter:    $ADAPTER
    model:      $MODEL
    repo:       $REPO_ROOT
EOF

# --- uv + deps ---
if ! command -v uv >/dev/null 2>&1; then
    echo "==> uv not found; installing via astral.sh"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
echo "==> uv sync"
cd "$REPO_ROOT"
uv sync --quiet

# --- broker reachability ---
echo "==> Probing $BROKER_URL/health"
if ! curl -sf "$BROKER_URL/health" >/dev/null; then
    echo "error: $BROKER_URL/health unreachable" >&2
    exit 1
fi

# --- api key validity ---
echo "==> Verifying API key (GET /agents/me)"
HTTP_CODE=$(curl -sS -o /tmp/mab-worker-me.json -w '%{http_code}' \
    -H "Authorization: Bearer $API_KEY" \
    "$BROKER_URL/api/v1/agents/me")
if [[ "$HTTP_CODE" != "200" ]]; then
    echo "error: GET /agents/me returned HTTP $HTTP_CODE" >&2
    cat /tmp/mab-worker-me.json >&2 2>/dev/null || true
    rm -f /tmp/mab-worker-me.json
    exit 1
fi
AGENT_NAME=$(python3 -c "import json; print(json.load(open('/tmp/mab-worker-me.json'))['name'])" 2>/dev/null || echo "?")
rm -f /tmp/mab-worker-me.json
echo "    OK — agent: $AGENT_NAME"

# --- build Environment= lines ---
ENV_LINES=()
ENV_LINES+=("Environment=\"MAB_BROKER_URL=$BROKER_URL\"")
ENV_LINES+=("Environment=\"MAB_API_KEY=$API_KEY\"")
ENV_LINES+=("Environment=\"MAB_ADAPTER=$ADAPTER\"")
ENV_LINES+=("Environment=\"MAB_MODEL=$MODEL\"")
ENV_LINES+=("Environment=\"MAB_PER_TASK_TIMEOUT=$PER_TASK_TIMEOUT\"")
ENV_LINES+=("Environment=\"MAB_WAIT_FOR_TASK_TIMEOUT=$WAIT_FOR_TASK_TIMEOUT\"")
[[ -n "$CAPABILITIES" ]] && ENV_LINES+=("Environment=\"MAB_CAPABILITIES=$CAPABILITIES\"")
[[ -n "$SYSTEM_PROMPT" ]] && ENV_LINES+=("Environment=\"MAB_SYSTEM_PROMPT=$SYSTEM_PROMPT\"")

case "$ADAPTER" in
    anthropic)
        ENV_LINES+=("Environment=\"ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY_V\"")
        ENV_LINES+=("Environment=\"ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL\"")
        ;;
    ollama)
        ENV_LINES+=("Environment=\"OLLAMA_BASE_URL=$OLLAMA_BASE_URL\"")
        [[ -n "$OLLAMA_API_KEY_V" ]] && ENV_LINES+=("Environment=\"OLLAMA_API_KEY=$OLLAMA_API_KEY_V\"")
        ;;
    gemini)
        ENV_LINES+=("Environment=\"GEMINI_API_KEY=$GEMINI_API_KEY_V\"")
        ENV_LINES+=("Environment=\"GEMINI_BASE_URL=$GEMINI_BASE_URL\"")
        ;;
    claude-cli)
        ENV_LINES+=("Environment=\"CLAUDE_BIN=$CLAUDE_BIN\"")
        ;;
esac

EXEC_FLAGS=""
$NO_SKIP_PERMISSIONS && EXEC_FLAGS=" --no-skip-permissions"

# --- write unit ---
mkdir -p "$(dirname "$UNIT_PATH")"
{
    cat <<EOF
[Unit]
Description=mab-worker daemon ($UNIT_BASENAME adapter=$ADAPTER model=$MODEL)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
EOF
    printf '%s\n' "${ENV_LINES[@]}"
    cat <<EOF
ExecStart=$REPO_ROOT/.venv/bin/mab-worker$EXEC_FLAGS
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
} > "$UNIT_PATH"
chmod 600 "$UNIT_PATH"
echo "==> Wrote $UNIT_PATH (chmod 600 — contains secrets)"

# --- linger ---
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "^Linger=yes"; then
    echo "==> Enabling linger for $USER (one-time sudo)"
    sudo loginctl enable-linger "$USER"
fi

# --- enable + (re)start ---
systemctl --user daemon-reload
if systemctl --user is-enabled "$UNIT_NAME" >/dev/null 2>&1; then
    echo "==> systemctl --user restart $UNIT_NAME"
    systemctl --user restart "$UNIT_NAME"
else
    echo "==> systemctl --user enable --now $UNIT_NAME"
    systemctl --user enable --now "$UNIT_NAME"
fi

# --- wait for online ---
echo -n "==> Waiting for worker to come online "
ready=false
for _ in $(seq 1 20); do
    status=$(curl -sf -H "Authorization: Bearer $API_KEY" \
        "$BROKER_URL/api/v1/agents/me" 2>/dev/null \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
    if [[ "$status" == "online" ]]; then
        ready=true; echo "OK"; break
    fi
    echo -n "."
    sleep 0.5
done

if ! $ready; then
    echo
    echo "warn: worker not yet 'online' in broker (still launching?). Check:" >&2
    echo "  systemctl --user status $UNIT_NAME" >&2
    echo "  journalctl --user -u $UNIT_NAME -n 50 --no-pager" >&2
    exit 0
fi

cat <<EOF

✓ Worker daemon installed and running.

  Service:   systemctl --user status $UNIT_NAME
  Logs:      journalctl --user -u $UNIT_NAME -f
  Restart:   systemctl --user restart $UNIT_NAME
  Stop:      systemctl --user stop $UNIT_NAME
  Update:    ./deploy/update.sh && systemctl --user restart $UNIT_NAME

  Identity:  $AGENT_NAME  (model=$MODEL via adapter=$ADAPTER)

To run multiple workers on this host, pass --name <suffix> per worker
(e.g. --name opus-cloud → mab-worker-opus-cloud.service).
EOF
