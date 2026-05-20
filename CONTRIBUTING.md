# Contributing to multi-agent-broker

Thank you for your interest in contributing.

## Developer Certificate of Origin (DCO)

All contributions must be signed off with the DCO. By signing off you certify that:

1. The contribution was created in whole or in part by you, and you have the right to submit it under the Apache 2.0 license.
2. The contribution is based on a previous work with compatible license.
3. You understand and agree the contribution is public and may be redistributed under Apache 2.0.

**Sign off every commit:**

```bash
git commit -s -m "Your commit message"
```

This adds `Signed-off-by: Your Name <your@email.com>` to the commit.

## Legal compliance

This project was developed entirely on personal time using personal devices. All contributors must confirm the same. Do not include any code derived from employer work product or code covered by an employment IP agreement.

## Code style

- Python 3.11+, no external formatter enforced (PEP 8 intent)
- No comments explaining *what* code does — only *why* when non-obvious
- No backwards-compatibility shims; change callers instead
- Tests for new broker routes / MCP tools / shared protocol changes are expected

## Pull requests

1. Fork and create a branch from `main`
2. Add tests if touching `broker/` or `mcp_server/` logic
3. Run `uv run pytest` before opening PR
4. Reference the relevant Phase if the change crosses scope (Phase 2 channels, Phase 3 SDK adapters, etc.)

## License

Apache 2.0 — see [LICENSE](LICENSE)
