#!/usr/bin/env bash
set -euo pipefail

# One-shot installer for multi-agent-broker on a single Linux PC.
# Runs the broker as a systemd --user service so day-to-day ops don't need sudo.
# Sudo is asked once for `loginctl enable-linger` (keeps the service alive
# when the user is not logged in interactively).

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYPROJECT="$REPO_ROOT/pyproject.toml"

if [[ ! -f "$PYPROJECT" ]] || ! grep -q "multi-agent-broker" "$PYPROJECT"; then
    echo "error: run from a multi-agent-broker checkout (no pyproject.toml at $REPO_ROOT)" >&2
    exit 1
fi

echo "==> Repo: $REPO_ROOT"

# --- 1. uv ---
if ! command -v uv >/dev/null 2>&1; then
    echo "==> uv not found; installing via astral.sh..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        echo "error: uv install completed but binary not on PATH" >&2
        exit 1
    fi
fi
echo "==> uv: $(uv --version)"

# --- 2. dependencies ---
echo "==> uv sync"
cd "$REPO_ROOT"
uv sync

# --- 3. data dir ---
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/multi-agent-broker"
mkdir -p "$DATA_DIR"
echo "==> Data dir: $DATA_DIR"

# --- 4. port pre-flight ---
PORT="${MAB_PORT:-8420}"
if command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | awk '{print $4}' | grep -qE ":${PORT}$"; then
    echo "warn: port $PORT already in use; service may fail to start" >&2
fi

# --- 5. systemd user unit ---
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/mab-broker.service"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=multi-agent-broker
After=network.target

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
Environment="MAB_HOST=0.0.0.0"
Environment="MAB_PORT=$PORT"
Environment="MAB_DB_PATH=$DATA_DIR/db.sqlite"
ExecStart=$REPO_ROOT/.venv/bin/mab-broker serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
echo "==> Unit: $UNIT_PATH"

# --- 6. linger (so service survives logout) ---
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "^Linger=yes"; then
    echo "==> Enabling linger for $USER (one-time sudo)"
    sudo loginctl enable-linger "$USER"
fi

# --- 7. enable + start ---
systemctl --user daemon-reload
systemctl --user enable --now mab-broker.service

# --- 8. wait for /health ---
echo -n "==> Waiting for broker readiness "
ready=false
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        ready=true
        echo "OK"
        break
    fi
    echo -n "."
    sleep 0.5
done

if ! $ready; then
    echo
    echo "error: broker did not become healthy within 15s. Check:" >&2
    echo "  systemctl --user status mab-broker" >&2
    echo "  journalctl --user -u mab-broker -n 50" >&2
    exit 1
fi

# --- 9. next steps ---
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
LAN_IP="${LAN_IP:-<this-host>}"

cat <<EOF

✓ multi-agent-broker is running.

  Service:   systemctl --user status mab-broker
  Logs:      journalctl --user -u mab-broker -f
  Restart:   systemctl --user restart mab-broker
  Health:    curl http://127.0.0.1:${PORT}/health
  Listen on: http://${LAN_IP}:${PORT}  (LAN-accessible)

To register an agent and print its API key:

  $REPO_ROOT/.venv/bin/mab-broker gen-key --name <agent-name>

On the agent machine, add to its .mcp.json:

  {
    "mcpServers": {
      "mab": {
        "command": "$REPO_ROOT/.venv/bin/mab-agent",
        "args": [
          "--broker-url", "http://${LAN_IP}:${PORT}",
          "--api-key", "<paste-key-here>"
        ]
      }
    }
  }

Firewall (open port if blocked):
  sudo ufw allow ${PORT}                                                              # Debian/Ubuntu
  sudo firewall-cmd --permanent --add-port=${PORT}/tcp && sudo firewall-cmd --reload  # Fedora/RHEL
EOF
