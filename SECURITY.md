# Security Policy

## Scope

multi-agent-broker (Phase 1) is intended for trusted internal networks. Public-internet deployment is the goal of Phase 4 (TLS / wss / JWT / IP allowlist) — until that ships, treat this as lab software.

**Known non-production aspects (Phase 1):**
- HTTP only — no TLS termination. Use `ssh -L`, WireGuard, or Tailscale if you need to cross networks
- API keys are bearer tokens — anyone who can read an agent's `.mcp.json` can act as that agent
- No rate limiting on the message bus — a runaway agent can flood the broker
- SQLite is single-host; loss of the broker host loses message history

These are acceptable tradeoffs for internal use. Do not deploy this on untrusted networks until Phase 4 lands.

## Reporting a vulnerability

If you find a security issue relevant to the framework's design (e.g., a way to escalate access via a malformed envelope, recover a hashed API key, or cause the broker to leak state across agents), please open a GitHub issue marked **[SECURITY]**.

For vulnerabilities in dependencies (FastAPI, websockets, mcp, etc.), report directly to those upstream projects.

## Supported versions

Only the latest commit on `main` is supported.
