# Deployment — multi-agent-broker

One-shot install for a single Linux PC. Runs the broker as a systemd **user service** — no dedicated `mab` system user, no writes under `/etc`. Sudo is required exactly once (for `loginctl enable-linger`).

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
git pull
uv sync
systemctl --user restart mab-broker
```

Or just re-run `./deploy/install.sh` — it's idempotent.

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
