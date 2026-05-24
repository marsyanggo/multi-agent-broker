"""Adapter that spawns `codex exec <prompt>` as a subprocess.

OpenAI's Codex CLI plays the same role for ChatGPT subscribers that
`claude -p` plays for Claude Max subscribers — non-interactive prompt
execution backed by an existing OAuth login rather than a separate API
key. As of 2026-05 Codex CLI is included in ChatGPT Free / Go / Plus /
Pro / Business / Enterprise plans, so a free ChatGPT account can host a
worker without paying for OpenAI API credits.

Each task spawns a fresh `codex exec` invocation, captures stdout, and
exits. The daemon owns broker interaction; the spawned codex just
answers the prompt.

Caveats:
- Adds ~3-5s cold-start latency per task vs a pure-API adapter
- Auth has to be set up on the worker host beforehand (`codex login`
  via device-code flow for headless boxes, or browser OAuth) — adapter
  setup() only checks the binary is callable, not that auth is valid
- `--skip-git-repo-check` is set by default (broker workdir often
  isn't a git repo); set `skip_git_repo_check=False` to require it
"""
from __future__ import annotations

import asyncio
from typing import Any

from mab.shared.models import Task
from mab.worker.adapters.base import LLMAdapter, LLMError


class CodexCLIAdapter(LLMAdapter):
    name = "codex-cli"

    def __init__(
        self,
        *,
        model: str,
        prompt_template: str = "{description}",
        extra_args: list[str] | None = None,
        codex_bin: str = "codex",
        skip_git_repo_check: bool = True,
    ):
        self.model = model
        self.prompt_template = prompt_template
        self.extra_args = list(extra_args) if extra_args else []
        self.codex_bin = codex_bin
        self.skip_git_repo_check = skip_git_repo_check

    async def setup(self) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.codex_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise LLMError(
                f"codex CLI not found at {self.codex_bin!r} — install OpenAI Codex CLI or pass --codex-bin"
            ) from e
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise LLMError(
                f"`{self.codex_bin} --version` exit {proc.returncode}: {stderr.decode()[:200]}"
            )

    async def run_task(self, task: Task) -> str:
        prompt = self.prompt_template.format(
            description=task.description,
            title=task.title,
            id=task.id,
        )
        # `codex exec <prompt>` is the non-interactive form. Model is set
        # via -m. We always request the final-message default output
        # (stdout text); --json mode is for structured event streaming,
        # not needed when we just want the answer.
        args: list[str] = [self.codex_bin, "exec", "-m", self.model]
        if self.skip_git_repo_check:
            args.append("--skip-git-repo-check")
        args.extend(self.extra_args)
        args.append(prompt)

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
                    f"codex CLI not found at {self.codex_bin!r}"
                ) from e

            stdout_bytes, stderr_bytes = await proc.communicate()
        except asyncio.CancelledError:
            # Daemon timeout / shutdown — make sure we don't leak a subprocess.
            await self._kill_subprocess(proc)
            raise

        if proc.returncode != 0:
            stderr_tail = stderr_bytes.decode(errors="replace")[-500:].strip()
            stdout_tail = stdout_bytes.decode(errors="replace")[-500:].strip()
            diag = stderr_tail or stdout_tail or "<no output>"
            stream = "stderr" if stderr_tail else "stdout" if stdout_tail else "neither"
            raise LLMError(
                f"`codex exec` exit {proc.returncode} ({stream}): {diag}"
            )

        result = stdout_bytes.decode(errors="replace").strip()
        if not result:
            raise LLMError("`codex exec` returned empty stdout")
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
