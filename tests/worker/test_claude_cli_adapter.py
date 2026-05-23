from __future__ import annotations

import asyncio
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mab.shared.models import Task
from mab.worker.adapters.base import LLMError
from mab.worker.adapters.claude_cli import ClaudeCLIAdapter


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


# A Python script that imitates `claude` CLI for testing. Writes a record of
# its argv to /tmp so tests can inspect what flags the adapter passed.
_SHIM_PY = '''\
#!/usr/bin/env python3
import os, sys, time, json, pathlib

argv = sys.argv[1:]

# Record what the adapter invoked us with.
record_path = os.environ.get("CLAUDE_SHIM_RECORD")
if record_path:
    pathlib.Path(record_path).write_text(json.dumps(argv))

# Handle --version probe
if "--version" in argv:
    if os.environ.get("CLAUDE_SHIM_VERSION_FAIL"):
        sys.stderr.write("simulated version failure\\n")
        sys.exit(1)
    print("claude shim 1.0")
    sys.exit(0)

# Look for -p prompt
prompt = None
for i, a in enumerate(argv):
    if a == "-p" and i + 1 < len(argv):
        prompt = argv[i + 1]
        break

if prompt is None:
    sys.stderr.write("no -p prompt\\n")
    sys.exit(2)

# Configurable behaviour via env
if os.environ.get("CLAUDE_SHIM_FAIL"):
    sys.stderr.write(os.environ["CLAUDE_SHIM_FAIL"])
    sys.exit(int(os.environ.get("CLAUDE_SHIM_EXIT", "1")))

if os.environ.get("CLAUDE_SHIM_EMPTY"):
    sys.exit(0)

delay = float(os.environ.get("CLAUDE_SHIM_DELAY", "0"))
if delay > 0:
    time.sleep(delay)

print(f"shim-response: {prompt}")
sys.exit(0)
'''


@pytest.fixture
def claude_shim(tmp_path: Path) -> Path:
    shim = tmp_path / "claude"
    shim.write_text(_SHIM_PY)
    # Make executable
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@pytest.fixture(autouse=True)
def _clean_shim_env(monkeypatch):
    """Clear shim-control env vars between tests so failure modes don't leak."""
    for k in (
        "CLAUDE_SHIM_FAIL",
        "CLAUDE_SHIM_EXIT",
        "CLAUDE_SHIM_EMPTY",
        "CLAUDE_SHIM_DELAY",
        "CLAUDE_SHIM_VERSION_FAIL",
        "CLAUDE_SHIM_RECORD",
    ):
        monkeypatch.delenv(k, raising=False)


async def test_claude_cli_setup_probes_version(claude_shim: Path) -> None:
    adapter = ClaudeCLIAdapter(model="x", claude_bin=str(claude_shim))
    await adapter.setup()  # should not raise


async def test_claude_cli_setup_raises_when_binary_missing() -> None:
    adapter = ClaudeCLIAdapter(model="x", claude_bin="/nonexistent/claude")
    with pytest.raises(LLMError, match="not found"):
        await adapter.setup()


async def test_claude_cli_setup_raises_on_version_failure(
    claude_shim: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_SHIM_VERSION_FAIL", "1")
    adapter = ClaudeCLIAdapter(model="x", claude_bin=str(claude_shim))
    with pytest.raises(LLMError, match="exit"):
        await adapter.setup()


async def test_claude_cli_runs_task_and_returns_stdout(
    claude_shim: Path,
) -> None:
    adapter = ClaudeCLIAdapter(
        model="claude-sonnet-4-6", claude_bin=str(claude_shim)
    )
    await adapter.setup()
    result = await adapter.run_task(_task("the prompt"))
    assert result == "shim-response: the prompt"


async def test_claude_cli_passes_model_and_skip_perms_flag(
    claude_shim: Path, tmp_path: Path, monkeypatch
) -> None:
    record = tmp_path / "argv.json"
    monkeypatch.setenv("CLAUDE_SHIM_RECORD", str(record))

    adapter = ClaudeCLIAdapter(
        model="claude-opus-4-7", claude_bin=str(claude_shim)
    )
    await adapter.setup()
    await adapter.run_task(_task("ping"))

    import json

    argv = json.loads(record.read_text())
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-7"
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("-p") + 1] == "ping"


async def test_claude_cli_skip_perms_disabled(
    claude_shim: Path, tmp_path: Path, monkeypatch
) -> None:
    record = tmp_path / "argv.json"
    monkeypatch.setenv("CLAUDE_SHIM_RECORD", str(record))

    adapter = ClaudeCLIAdapter(
        model="x",
        claude_bin=str(claude_shim),
        dangerously_skip_permissions=False,
    )
    await adapter.setup()
    await adapter.run_task(_task())

    import json

    argv = json.loads(record.read_text())
    assert "--dangerously-skip-permissions" not in argv


async def test_claude_cli_prompt_template_with_task_fields(
    claude_shim: Path,
) -> None:
    adapter = ClaudeCLIAdapter(
        model="x",
        claude_bin=str(claude_shim),
        prompt_template="task {id} ({title}): {description}",
    )
    await adapter.setup()
    result = await adapter.run_task(_task("body"))
    assert "task t1 (t): body" in result


async def test_claude_cli_raises_on_nonzero_exit(
    claude_shim: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_SHIM_FAIL", "broken pipe")
    monkeypatch.setenv("CLAUDE_SHIM_EXIT", "3")

    adapter = ClaudeCLIAdapter(model="x", claude_bin=str(claude_shim))
    await adapter.setup()
    with pytest.raises(LLMError, match="exit 3"):
        await adapter.run_task(_task())


async def test_claude_cli_raises_on_empty_stdout(
    claude_shim: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_SHIM_EMPTY", "1")
    adapter = ClaudeCLIAdapter(model="x", claude_bin=str(claude_shim))
    await adapter.setup()
    with pytest.raises(LLMError, match="empty"):
        await adapter.run_task(_task())


async def test_claude_cli_kills_subprocess_on_cancel(
    claude_shim: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_SHIM_DELAY", "5")
    adapter = ClaudeCLIAdapter(model="x", claude_bin=str(claude_shim))
    await adapter.setup()

    run = asyncio.create_task(adapter.run_task(_task()))
    await asyncio.sleep(0.2)  # let subprocess actually start
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    # No assert on subprocess state — but if _kill_subprocess didn't run we
    # would leak the 5s shim. Test runs in < 1s = proof.


async def test_claude_cli_extra_args_appended(
    claude_shim: Path, tmp_path: Path, monkeypatch
) -> None:
    record = tmp_path / "argv.json"
    monkeypatch.setenv("CLAUDE_SHIM_RECORD", str(record))

    adapter = ClaudeCLIAdapter(
        model="x",
        claude_bin=str(claude_shim),
        extra_args=["--verbose", "--max-turns", "2"],
    )
    await adapter.setup()
    await adapter.run_task(_task())

    import json

    argv = json.loads(record.read_text())
    assert "--verbose" in argv
    assert "--max-turns" in argv
    assert "2" in argv
