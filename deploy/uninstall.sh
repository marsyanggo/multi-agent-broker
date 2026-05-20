#!/usr/bin/env bash
set -euo pipefail

UNIT_PATH="$HOME/.config/systemd/user/mab-broker.service"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/multi-agent-broker"

systemctl --user disable --now mab-broker.service 2>/dev/null || true
rm -f "$UNIT_PATH"
systemctl --user daemon-reload

echo "✓ Removed mab-broker service unit ($UNIT_PATH)."
echo
echo "Data directory NOT removed: $DATA_DIR"
echo "  (delete manually to wipe agent registrations and message history)"
echo
echo "Linger NOT disabled. If you want to revert that too:"
echo "  sudo loginctl disable-linger $USER"
