#!/usr/bin/env bash
set -euo pipefail

# One-shot updater for mab-worker daemons on a host where setup-worker.sh
# has already run. Pulls latest, syncs deps, restarts every
# mab-worker*.service unit found on this user (or just one via --name).
#
# Safe-by-default: bails out on uncommitted changes or non-fast-forward
# pull. Mirrors deploy/update.sh shape, just targets worker units instead
# of mab-broker.service.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYPROJECT="$REPO_ROOT/pyproject.toml"
UNIT_DIR="$HOME/.config/systemd/user"

NAME=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) NAME="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--name SUFFIX]

Pull latest, uv sync, restart mab-worker[-SUFFIX].service.

Without --name, restarts ALL mab-worker*.service units this user has
installed (handles multi-daemon hosts cleanly).

Examples:
  $0                    # restart every mab-worker* unit
  $0 --name gemini      # restart only mab-worker-gemini.service
EOF
            exit 0 ;;
        *) echo "error: unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ ! -f "$PYPROJECT" ]] || ! grep -q "multi-agent-broker" "$PYPROJECT"; then
    echo "error: run from a multi-agent-broker checkout (no pyproject.toml at $REPO_ROOT)" >&2
    exit 1
fi

# --- 1. discover units to restart ---
UNITS=()
if [[ -n "$NAME" ]]; then
    UNIT_NAME="mab-worker-${NAME}.service"
    if [[ ! -f "$UNIT_DIR/$UNIT_NAME" ]]; then
        echo "error: $UNIT_DIR/$UNIT_NAME missing — run ./deploy/setup-worker.sh first." >&2
        exit 1
    fi
    UNITS+=("$UNIT_NAME")
else
    shopt -s nullglob
    for f in "$UNIT_DIR"/mab-worker*.service; do
        UNITS+=("$(basename "$f")")
    done
    shopt -u nullglob
    if [[ ${#UNITS[@]} -eq 0 ]]; then
        echo "error: no mab-worker*.service units in $UNIT_DIR" >&2
        echo "       Run ./deploy/setup-worker.sh first." >&2
        exit 1
    fi
fi

echo "==> Will restart: ${UNITS[*]}"

cd "$REPO_ROOT"

# --- 2. clean tree check ---
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

# --- 3. fetch + ff-only pull ---
echo "==> Fetching..."
git fetch --quiet

if ! git merge-base --is-ancestor "$OLD_HEAD" "@{u}" 2>/dev/null; then
    echo "error: $BRANCH has diverged from its upstream — refusing to overwrite local commits." >&2
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

# --- 4. sync deps ---
echo "==> uv sync"
uv sync --quiet

# --- 5. restart units ---
for u in "${UNITS[@]}"; do
    echo "==> Restarting $u"
    systemctl --user restart "$u"
done

# --- 6. verify each came back online ---
# Hit broker /agents/me with each unit's MAB_API_KEY to confirm
# heartbeat is alive. 10s poll budget per unit.
for u in "${UNITS[@]}"; do
    UNIT_PATH="$UNIT_DIR/$u"
    BROKER_URL=$(grep -oE 'MAB_BROKER_URL="[^"]+"' "$UNIT_PATH" | head -1 | sed 's/.*="//;s/"$//')
    API_KEY=$(grep -oE 'MAB_API_KEY="[^"]+"' "$UNIT_PATH" | head -1 | sed 's/.*="//;s/"$//')
    if [[ -z "$BROKER_URL" || -z "$API_KEY" ]]; then
        echo "warn: $u — couldn't read broker URL or api-key from unit, skipping online check" >&2
        continue
    fi

    echo -n "==> $u: waiting for /agents/me online "
    ready=false
    for _ in $(seq 1 20); do
        status=$(curl -sf -H "Authorization: Bearer $API_KEY" \
            "$BROKER_URL/api/v1/agents/me" 2>/dev/null \
            | grep -oE '"status":"[a-z]+"' | head -1 | cut -d'"' -f4 || true)
        if [[ "$status" == "online" || "$status" == "idle" ]]; then
            ready=true
            echo "OK ($status)"
            break
        fi
        echo -n "."
        sleep 0.5
    done
    if ! $ready; then
        echo
        echo "warn: $u didn't go online within 10s. Check:" >&2
        echo "  systemctl --user status $u" >&2
        echo "  journalctl --user -u $u -n 50" >&2
    fi
done

# --- 7. summary ---
echo
if $NO_CHANGES; then
    echo "✓ No code changes; deps verified, ${#UNITS[@]} unit(s) restarted."
else
    echo "✓ Updated $OLD_HEAD → $NEW_HEAD"
    echo "  $(git log --oneline "$OLD_HEAD..$NEW_HEAD" | wc -l | tr -d ' ') commits applied. ${#UNITS[@]} unit(s) restarted."
fi
