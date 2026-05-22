#!/usr/bin/env bash
set -euo pipefail

# One-shot updater for multi-agent-broker on a host where install.sh has
# already run. Pulls latest, syncs deps, restarts the systemd user service,
# and waits for /health. Safe-by-default: bails out on uncommitted changes
# or on a non-fast-forward pull.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYPROJECT="$REPO_ROOT/pyproject.toml"
UNIT_PATH="$HOME/.config/systemd/user/mab-broker.service"

if [[ ! -f "$PYPROJECT" ]] || ! grep -q "multi-agent-broker" "$PYPROJECT"; then
    echo "error: run from a multi-agent-broker checkout (no pyproject.toml at $REPO_ROOT)" >&2
    exit 1
fi

if [[ ! -f "$UNIT_PATH" ]]; then
    echo "error: $UNIT_PATH missing — broker isn't installed yet." >&2
    echo "       Run ./deploy/install.sh first." >&2
    exit 1
fi

cd "$REPO_ROOT"

# --- 1. clean tree check ---
if [[ -n "$(git status --porcelain)" ]]; then
    echo "error: working tree has uncommitted changes. Stash or commit first:" >&2
    echo >&2
    git status -sb >&2
    echo >&2
    echo "  git stash push -u    # set aside everything (re-apply later with git stash pop)" >&2
    exit 1
fi

OLD_HEAD="$(git rev-parse HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "==> Branch: $BRANCH  (was at $(git log -1 --pretty=format:'%h %s'))"

# --- 2. fetch + ff-only pull ---
echo "==> Fetching..."
git fetch --quiet

if ! git merge-base --is-ancestor "$OLD_HEAD" "@{u}" 2>/dev/null; then
    echo "error: $BRANCH has diverged from its upstream — refusing to overwrite local commits." >&2
    echo "       Resolve manually (rebase / merge) then re-run." >&2
    exit 1
fi

if [[ "$OLD_HEAD" == "$(git rev-parse @{u})" ]]; then
    echo "==> Already up to date; nothing to pull."
    NO_CHANGES=true
else
    echo "==> Incoming commits:"
    git log --oneline "$OLD_HEAD..@{u}" | sed 's/^/    /'
    git pull --ff-only --quiet
    NO_CHANGES=false
fi

NEW_HEAD="$(git rev-parse HEAD)"

# --- 3. sync deps ---
echo "==> uv sync"
uv sync --quiet

# --- 4. restart service ---
echo "==> Restarting mab-broker.service"
systemctl --user restart mab-broker.service

# --- 5. wait for /health ---
PORT="$(grep -oE 'MAB_PORT=[0-9]+' "$UNIT_PATH" | head -1 | cut -d= -f2)"
PORT="${PORT:-8420}"

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
    echo "  (You may want to roll back: git reset --hard $OLD_HEAD && uv sync && systemctl --user restart mab-broker)" >&2
    exit 1
fi

# --- 6. summary ---
if $NO_CHANGES; then
    echo
    echo "✓ No code changes; deps verified, service restarted, /health OK."
else
    echo
    echo "✓ Updated $OLD_HEAD → $NEW_HEAD"
    echo "  $(git log --oneline "$OLD_HEAD..$NEW_HEAD" | wc -l | tr -d ' ') commits applied. Service healthy."
fi
