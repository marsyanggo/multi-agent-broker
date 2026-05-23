"""Adapter that spawns `claude -p <prompt>` as a subprocess.

Use this for tasks that need Claude Code's tool ecosystem (Bash / Edit /
Read / web search) rather than a pure LLM call. Each task spawns a fresh
Claude Code session, runs the prompt, and exits — no cross-task state. The
daemon owns broker interaction; the spawned `claude` just answers the prompt.

Caveats:
- Adds ~3-5s cold-start latency per task vs a pure-API adapter.
- The spawned `claude` reads the daemon host's `~/.claude.json` (so it has
  whatever MCP servers are wired there). For most uses this is fine, but be
  aware the spawned claude *could* call mab tools too — keep prompts focused.
- `--dangerously-skip-permissions` is set by default since the daemon already
  runs unattended; set `dangerously_skip_permissions=False` if you want each
  task's tool calls to require explicit allowlist via Claude Code settings.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mab.shared.models import Task
from mab.worker.adapters.base import LLMAdapter, LLMError


class ClaudeCLIAdapter(LLMAdapter):
    name = "claude-cli"

    def __init__(
        self,
        *,
        model: str,
        prompt_template: str = "{description}",
        extra_args: list[str] | None = None,
        claude_bin: str = "claude",
        dangerously_skip_permissions: bool = True,
    ):
        self.model = model
        self.prompt_template = prompt_template
        self.extra_args = list(extra_args) if extra_args else []
        self.claude_bin = claude_bin
        self.skip_permissions = dangerously_skip_permissions

    async def setup(self) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.claude_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise LLMError(
                f"claude CLI not found at {self.claude_bin!r} — install Claude Code or pass --claude-bin"
            ) from e
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise LLMError(
                f"`{self.claude_bin} --version` exit {proc.returncode}: {stderr.decode()[:200]}"
            )

    async def run_task(self, task: Task) -> str:
        prompt = self.prompt_template.format(
            description=task.description,
            title=task.title,
            id=task.id,
        )
        args: list[str] = [self.claude_bin, "-p", prompt, "--model", self.model]
        if self.skip_permissions:
            args.append("--dangerously-skip-permissions")
        args.extend(self.extra_args)

        proc: asyncio.subprocess.Process | None = None
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as e:
                raise LLMError(
                    f"claude CLI not found at {self.claude_bin!r}"
                ) from e

            stdout_bytes, stderr_bytes = await proc.communicate()
        except asyncio.CancelledError:
            # Daemon timeout / shutdown — make sure we don't leak a subprocess.
            await self._kill_subprocess(proc)
            raise

        if proc.returncode != 0:
            tail = stderr_bytes.decode(errors="replace")[-500:]
            raise LLMError(
                f"`claude -p` exit {proc.returncode}: {tail.strip() or '<no stderr>'}"
            )

        result = stdout_bytes.decode(errors="replace").strip()
        if not result:
            raise LLMError("`claude -p` returned empty stdout")
        return result

    @staticmethod
    async def _kill_subprocess(
        proc: asyncio.subprocess.Process | None,
    ) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
