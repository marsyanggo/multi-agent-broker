"""CodexCLIAdapter unit tests using a Python shim that mimics `codex exec`.

Same harness pattern as test_claude_cli_adapter — no real OpenAI Codex
CLI installed, no real ChatGPT account needed. The shim records its
argv to a JSON file so tests can assert which flags the adapter passed.
"""
from __future__ import annotations

import asyncio
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mab.shared.models import Task
from mab.worker.adapters.base import LLMError
from mab.worker.adapters.codex_cli import CodexCLIAdapter


def _task(description: str = "say hi") -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id="t1",
        title="t",
        description=description,
        created_by="me",
        created_at=now,
        updated_at=now,
    )


_SHIM_PY = '''\
#!/usr/bin/env python3
import os, sys, time, json, pathlib

argv = sys.argv[1:]

record_path = os.environ.get("CODEX_SHIM_RECORD")
if record_path:
    pathlib.Path(record_path).write_text(json.dumps(argv))

# --version probe
if "--version" in argv:
    if os.environ.get("CODEX_SHIM_VERSION_FAIL"):
        sys.stderr.write("simulated version failure\\n")
        sys.exit(1)
    print("codex shim 1.0")
    sys.exit(0)

# Real codex CLI takes: codex exec [-m MODEL] [--skip-git-repo-check] [...] PROMPT
# Find the "exec" subcommand
if argv[0] != "exec":
    sys.stderr.write(f"shim: unexpected subcommand {argv[0]!r}\\n")
    sys.exit(2)

# Prompt is the last positional after all flags. Walk argv skipping known
# flag pairs/singletons.
rest = argv[1:]
i = 0
flag_pairs = {"-m", "--model", "-o", "--output-schema"}
flag_singletons = {"--skip-git-repo-check", "--json"}
positional = []
while i < len(rest):
    a = rest[i]
    if a in flag_pairs:
        i += 2
    elif a in flag_singletons or a.startswith("--"):
        i += 1
    else:
        positional.append(a)
        i += 1
prompt = positional[-1] if positional else None

if prompt is None:
    sys.stderr.write("no prompt argument\\n")
    sys.exit(2)

if os.environ.get("CODEX_SHIM_FAIL"):
    sys.stderr.write(os.environ["CODEX_SHIM_FAIL"])
    sys.exit(int(os.environ.get("CODEX_SHIM_EXIT", "1")))

if os.environ.get("CODEX_SHIM_EMPTY"):
    sys.exit(0)

delay = float(os.environ.get("CODEX_SHIM_DELAY", "0"))
if delay > 0:
    time.sleep(delay)

print(f"shim-response: {prompt}")
sys.exit(0)
'''


@pytest.fixture
def codex_shim(tmp_path: Path) -> Path:
    shim = tmp_path / "codex"
    shim.write_text(_SHIM_PY)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@pytest.fixture(autouse=True)
def _clean_shim_env(monkeypatch):
    for k in (
        "CODEX_SHIM_FAIL",
        "CODEX_SHIM_EXIT",
        "CODEX_SHIM_EMPTY",
        "CODEX_SHIM_DELAY",
        "CODEX_SHIM_VERSION_FAIL",
        "CODEX_SHIM_RECORD",
    ):
        monkeypatch.delenv(k, raising=False)


async def test_codex_cli_setup_probes_version(codex_shim: Path) -> None:
    adapter = CodexCLIAdapter(model="x", codex_bin=str(codex_shim))
    await adapter.setup()


async def test_codex_cli_setup_raises_when_binary_missing() -> None:
    adapter = CodexCLIAdapter(model="x", codex_bin="/nonexistent/codex")
    with pytest.raises(LLMError, match="not found"):
        await adapter.setup()


async def test_codex_cli_setup_raises_on_version_failure(
    codex_shim: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CODEX_SHIM_VERSION_FAIL", "1")
    adapter = CodexCLIAdapter(model="x", codex_bin=str(codex_shim))
    with pytest.raises(LLMError, match="exit"):
        await adapter.setup()


async def test_codex_cli_runs_task_and_returns_stdout(codex_shim: Path) -> None:
    adapter = CodexCLIAdapter(model="gpt-5", codex_bin=str(codex_shim))
    await adapter.setup()
    result = await adapter.run_task(_task("the prompt"))
    assert result == "shim-response: the prompt"


async def test_codex_cli_passes_exec_subcommand_and_model(
    codex_shim: Path, tmp_path: Path, monkeypatch
) -> None:
    record = tmp_path / "argv.json"
    monkeypatch.setenv("CODEX_SHIM_RECORD", str(record))

    adapter = CodexCLIAdapter(model="gpt-5", codex_bin=str(codex_shim))
    await adapter.setup()
    await adapter.run_task(_task("ping"))

    import json
    argv = json.loads(record.read_text())
    assert argv[0] == "exec"
    assert "-m" in argv
    assert argv[argv.index("-m") + 1] == "gpt-5"
    assert "--skip-git-repo-check" in argv
    assert argv[-1] == "ping"


async def test_codex_cli_skip_git_check_disabled(
    codex_shim: Path, tmp_path: Path, monkeypatch
) -> None:
    record = tmp_path / "argv.json"
    monkeypatch.setenv("CODEX_SHIM_RECORD", str(record))

    adapter = CodexCLIAdapter(
        model="x", codex_bin=str(codex_shim), skip_git_repo_check=False
    )
    await adapter.setup()
    await adapter.run_task(_task())

    import json
    argv = json.loads(record.read_text())
    assert "--skip-git-repo-check" not in argv


async def test_codex_cli_prompt_template_with_task_fields(codex_shim: Path) -> None:
    adapter = CodexCLIAdapter(
        model="x",
        codex_bin=str(codex_shim),
        prompt_template="task {id} ({title}): {description}",
    )
    await adapter.setup()
    result = await adapter.run_task(_task("body"))
    assert "task t1 (t): body" in result


async def test_codex_cli_raises_on_nonzero_exit(
    codex_shim: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CODEX_SHIM_FAIL", "auth invalid")
    monkeypatch.setenv("CODEX_SHIM_EXIT", "3")
    adapter = CodexCLIAdapter(model="x", codex_bin=str(codex_shim))
    await adapter.setup()
    with pytest.raises(LLMError, match="exit 3"):
        await adapter.run_task(_task())


async def test_codex_cli_raises_on_empty_stdout(
    codex_shim: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CODEX_SHIM_EMPTY", "1")
    adapter = CodexCLIAdapter(model="x", codex_bin=str(codex_shim))
    await adapter.setup()
    with pytest.raises(LLMError, match="empty"):
        await adapter.run_task(_task())


async def test_codex_cli_kills_subprocess_on_cancel(
    codex_shim: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CODEX_SHIM_DELAY", "5")
    adapter = CodexCLIAdapter(model="x", codex_bin=str(codex_shim))
    await adapter.setup()

    run = asyncio.create_task(adapter.run_task(_task()))
    await asyncio.sleep(0.2)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run


async def test_codex_cli_extra_args_appended(
    codex_shim: Path, tmp_path: Path, monkeypatch
) -> None:
    record = tmp_path / "argv.json"
    monkeypatch.setenv("CODEX_SHIM_RECORD", str(record))

    adapter = CodexCLIAdapter(
        model="x",
        codex_bin=str(codex_shim),
        extra_args=["--json"],
    )
    await adapter.setup()
    await adapter.run_task(_task())

    import json
    argv = json.loads(record.read_text())
    assert "--json" in argv
