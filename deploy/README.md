# Deployment — multi-agent-broker

One-command install + one-command update for a single Linux PC. Runs the broker as a systemd **user service** — no dedicated `mab` system user, no writes under `/etc`. Sudo is required exactly once at install time (for `loginctl enable-linger`); updates are 100% non-sudo.

| Action | Command |
|--------|---------|
| Install broker | `./deploy/install.sh` |
| Wire Claude Code as agent | `./deploy/setup-agent.sh --broker-url ... --api-key ... [--model ...]` |
| Update broker | `./deploy/update.sh` |
| Uninstall broker (keeps DB) | `./deploy/uninstall.sh` |

## Prerequisites

- Linux with systemd (Debian/Ubuntu 22+, Fedora 38+, Arch, etc.)
- Outbound internet for `uv` install + dependency download
- One-time `sudo` for `loginctl enable-linger` (keeps the service alive when no one is logged in interactively)

## Install

```bash
git clone <repo-url>
cd multi-agent-broker
./deploy/install.sh
```

What the script does:
1. Installs `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`) if not already on PATH
2. `uv sync` — creates `.venv` in the repo, installs deps
3. Writes a systemd unit to `~/.config/systemd/user/mab-broker.service`
4. Enables linger so the service survives logout
5. `systemctl --user enable --now mab-broker.service`
6. Polls `/health` to confirm readiness
7. Prints next steps — `gen-key` and a ready-to-paste `.mcp.json` snippet

The script is idempotent — re-run it after `git pull` to apply updates without breakage.

## Register an agent

Two halves: **(a) generate a key on the broker host**, and **(b) wire Claude Code on whichever host wants to act as that agent** (could be the same box as the broker, or a remote machine).

### (a) Generate a key (run on broker host)

The installer sets a non-default `MAB_DB_PATH` for the broker (so data sits under XDG, not `~/.multi-agent-broker/`). When you run `gen-key` from the shell, you **must** pass the same env var, or the key is written to a different SQLite file and the broker will reject every connection with `invalid api key`:

```bash
MAB_DB_PATH=$HOME/.local/share/multi-agent-broker/db.sqlite \
  /path/to/multi-agent-broker/.venv/bin/mab-broker gen-key --name <agent-name>
```

Capture the printed `mab-ak-...` — it's only shown once.

### (b) Wire Claude Code's MCP config (run on the agent host)

Use `setup-agent.sh`. It probes the broker, validates the key, then writes the MCP entry to `~/.claude.json` (via `claude mcp add` if the CLI is on PATH, else direct JSON edit with a backup):

```bash
./deploy/setup-agent.sh \
  --broker-url http://192.168.1.100:8420 \
  --api-key   mab-ak-XXXXXXXXXXXXXXXX \
  --model     claude-opus-4-7              # optional; auto-derives capability tags
```

If this machine is a **dedicated worker host** (will run `/worker-mode`), also pass `--worker-host`:

```bash
./deploy/setup-agent.sh ... --worker-host
```

That additionally writes `<repo>/.claude/settings.local.json` with `permissions.defaultMode = "bypassPermissions"` so worker mode can execute Bash / Write / Edit calls without permission prompts. The `.local.json` file is gitignored — per-host opt-in. **Do not** pass this on a machine where you also use the same repo for interactive dev; the bypass applies to every Claude Code session opened in that directory.

Then **restart Claude Code on that host** — MCP servers only load at session startup. After restart, `claude mcp list` should show `mab: ✓ Connected`, and the `/lead-mode` / `/worker-mode` slash commands become usable.

`setup-agent.sh` is safe to re-run (idempotent) and never touches the broker DB — it only manipulates the local Claude config.

## Day-to-day ops

```bash
systemctl --user status mab-broker         # is it running?
systemctl --user restart mab-broker        # restart after config change
journalctl --user -u mab-broker -f         # tail logs
curl http://127.0.0.1:8420/health          # smoke test
```

## Layout

| Item | Path |
|------|------|
| Code + `.venv` | wherever you cloned the repo (uv puts venv inside) |
| Systemd unit | `~/.config/systemd/user/mab-broker.service` |
| Database | `~/.local/share/multi-agent-broker/db.sqlite` |
| Logs | journald (`journalctl --user -u mab-broker`) |

## Configuration overrides

The unit sets these defaults:

```
Environment="MAB_HOST=0.0.0.0"
Environment="MAB_PORT=8420"
Environment="MAB_DB_PATH=<XDG data dir>/multi-agent-broker/db.sqlite"
```

To change, edit the unit and reload:

```bash
$EDITOR ~/.config/systemd/user/mab-broker.service
systemctl --user daemon-reload
systemctl --user restart mab-broker
```

## Update

```bash
cd /path/to/multi-agent-broker
./deploy/update.sh
```

What it does (in order, aborts on any failure):

1. Refuses to run if the working tree is dirty (`git status` not clean) — your local changes are safe
2. `git fetch`, prints the incoming commit list
3. `git pull --ff-only` (refuses to overwrite local history)
4. `uv sync` (applies any new dependencies)
5. `systemctl --user restart mab-broker.service`
6. Polls `/health` until ready (or fails after 15s with rollback hint)
7. Prints `OLD_HEAD → NEW_HEAD` summary + commit count applied

Safe to re-run when there's nothing new — it'll print "Already up to date" and still verify deps + health.

If `update.sh` aborts, your previous SHA is still checked out and the service was never restarted — production is unchanged.

For manual control or recovery, the underlying commands are:

```bash
cd /path/to/multi-agent-broker
git pull --ff-only
uv sync
systemctl --user restart mab-broker
```

## Uninstall

```bash
./deploy/uninstall.sh
```

Removes the unit + stops the service. **Does not** delete the database — wipe `~/.local/share/multi-agent-broker/` manually if you want a clean slate.

## Firewall

The installer does NOT touch firewall config. If your distro has one and blocks 8420, open it yourself:

```bash
sudo ufw allow 8420                                                              # Debian/Ubuntu
sudo firewall-cmd --permanent --add-port=8420/tcp && sudo firewall-cmd --reload  # Fedora/RHEL
```

## Cross-host / multi-network

The broker binds `0.0.0.0:8420`, fine for LAN. For cross-network or public-internet exposure:

- **Easy today:** Tailscale / WireGuard — agents connect over the overlay network, no broker changes
- **Eventually:** Phase 4 of the project adds TLS / wss / JWT / IP allowlist
