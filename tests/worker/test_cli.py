from __future__ import annotations

import argparse

import pytest

from mab.worker.adapters.anthropic import AnthropicAdapter
from mab.worker.adapters.claude_cli import ClaudeCLIAdapter
from mab.worker.adapters.codex_cli import CodexCLIAdapter
from mab.worker.adapters.gemini import GeminiAdapter
from mab.worker.adapters.mock import MockAdapter
from mab.worker.adapters.ollama import OllamaAdapter
from mab.worker.cli import (
    ADAPTER_CHOICES,
    build_adapter,
    build_parser,
    parse_capabilities,
    validate,
)


def _parse(*argv: str, **env) -> argparse.Namespace:
    # build_parser reads env defaults at construction; we have to apply env
    # before calling.
    import os

    saved = {}
    try:
        for k, v in env.items():
            saved[k] = os.environ.get(k)
            os.environ[k] = v
        parser = build_parser()
        return parser.parse_args(list(argv))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_parse_capabilities_handles_whitespace_and_empty() -> None:
    assert parse_capabilities("a, b,  c ") == ["a", "b", "c"]
    assert parse_capabilities("") == []
    assert parse_capabilities("  ,  ") == []


def test_build_adapter_anthropic() -> None:
    args = _parse(
        "--broker-url", "http://x", "--api-key", "k", "--model", "claude-sonnet-4-6",
        "--adapter", "anthropic", "--anthropic-api-key", "sk-test",
        "--anthropic-max-tokens", "512",
    )
    adapter = build_adapter(args)
    assert isinstance(adapter, AnthropicAdapter)
    assert adapter.model == "claude-sonnet-4-6"
    assert adapter.api_key == "sk-test"
    assert adapter.max_tokens == 512


def test_build_adapter_ollama_uses_defaults() -> None:
    args = _parse(
        "--broker-url", "http://x", "--api-key", "k",
        "--model", "gpt-oss:120b-cloud",
        "--adapter", "ollama",
    )
    adapter = build_adapter(args)
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.base_url == "http://localhost:11434"


def test_build_adapter_ollama_cloud() -> None:
    args = _parse(
        "--broker-url", "http://x", "--api-key", "k",
        "--model", "gpt-oss:120b-cloud",
        "--adapter", "ollama",
        "--ollama-base-url", "https://ollama.com",
        "--ollama-api-key", "cloudkey",
        "--system-prompt", "be concise",
    )
    adapter = build_adapter(args)
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.base_url == "https://ollama.com"
    assert adapter.api_key == "cloudkey"
    assert adapter.system_prompt == "be concise"


def test_build_adapter_claude_cli() -> None:
    args = _parse(
        "--broker-url", "http://x", "--api-key", "k",
        "--model", "claude-opus-4-7",
        "--adapter", "claude-cli",
        "--claude-bin", "/usr/local/bin/claude",
        "--claude-prompt-template", "TASK {id}: {description}",
    )
    adapter = build_adapter(args)
    assert isinstance(adapter, ClaudeCLIAdapter)
    assert adapter.claude_bin == "/usr/local/bin/claude"
    assert adapter.prompt_template == "TASK {id}: {description}"
    assert adapter.skip_permissions is True


def test_build_adapter_claude_cli_no_skip_permissions() -> None:
    args = _parse(
        "--broker-url", "http://x", "--api-key", "k",
        "--model", "claude-opus-4-7",
        "--adapter", "claude-cli",
        "--no-skip-permissions",
    )
    adapter = build_adapter(args)
    assert isinstance(adapter, ClaudeCLIAdapter)
    assert adapter.skip_permissions is False


def test_build_adapter_codex_cli() -> None:
    args = _parse(
        "--broker-url", "http://x", "--api-key", "k",
        "--model", "gpt-5",
        "--adapter", "codex-cli",
        "--codex-bin", "/usr/local/bin/codex",
        "--codex-prompt-template", "{title}: {description}",
    )
    adapter = build_adapter(args)
    assert isinstance(adapter, CodexCLIAdapter)
    assert adapter.model == "gpt-5"
    assert adapter.codex_bin == "/usr/local/bin/codex"
    assert adapter.prompt_template == "{title}: {description}"
    assert adapter.skip_git_repo_check is True


def test_build_adapter_codex_cli_no_skip_git_check() -> None:
    args = _parse(
        "--broker-url", "http://x", "--api-key", "k",
        "--model", "gpt-5",
        "--adapter", "codex-cli",
        "--no-skip-git-repo-check",
    )
    adapter = build_adapter(args)
    assert isinstance(adapter, CodexCLIAdapter)
    assert adapter.skip_git_repo_check is False


def test_build_adapter_gemini() -> None:
    args = _parse(
        "--broker-url", "http://x", "--api-key", "k",
        "--model", "gemini-2.5-flash",
        "--adapter", "gemini",
        "--gemini-api-key", "test-key",
        "--gemini-max-output-tokens", "512",
        "--gemini-temperature", "0.4",
        "--system-prompt", "be terse",
    )
    adapter = build_adapter(args)
    assert isinstance(adapter, GeminiAdapter)
    assert adapter.model == "gemini-2.5-flash"
    assert adapter.api_key == "test-key"
    assert adapter.max_output_tokens == 512
    assert adapter.temperature == 0.4
    assert adapter.system_prompt == "be terse"


def test_build_adapter_mock() -> None:
    args = _parse(
        "--broker-url", "http://x", "--api-key", "k", "--model", "any",
        "--adapter", "mock", "--mock-response", "fixed answer",
    )
    adapter = build_adapter(args)
    assert isinstance(adapter, MockAdapter)


def test_validate_missing_api_key_exits() -> None:
    args = argparse.Namespace(
        api_key="", model="x", adapter="mock",
    )
    with pytest.raises(SystemExit):
        validate(args)


def test_validate_missing_model_exits() -> None:
    args = argparse.Namespace(
        api_key="k", model="", adapter="mock",
    )
    with pytest.raises(SystemExit):
        validate(args)


def test_validate_missing_adapter_exits() -> None:
    args = argparse.Namespace(
        api_key="k", model="x", adapter=None,
    )
    with pytest.raises(SystemExit):
        validate(args)


def test_validate_passes_with_all_required() -> None:
    args = argparse.Namespace(
        api_key="k", model="x", adapter="mock",
    )
    validate(args)  # no raise


def test_adapter_choices_match_build_adapter() -> None:
    # Sanity: every choice must produce an adapter
    for choice in ADAPTER_CHOICES:
        if choice == "anthropic":
            ns = _parse(
                "--broker-url", "http://x", "--api-key", "k",
                "--model", "x", "--adapter", choice,
                "--anthropic-api-key", "sk",
            )
        else:
            ns = _parse(
                "--broker-url", "http://x", "--api-key", "k",
                "--model", "x", "--adapter", choice,
            )
        assert build_adapter(ns).name == choice


def test_env_vars_supply_defaults() -> None:
    args = _parse(
        "--adapter", "mock",
        MAB_BROKER_URL="http://from-env",
        MAB_API_KEY="envkey",
        MAB_MODEL="envmodel",
        MAB_CAPABILITIES="extra-tag",
    )
    assert args.broker_url == "http://from-env"
    assert args.api_key == "envkey"
    assert args.model == "envmodel"
    assert parse_capabilities(args.capabilities) == ["extra-tag"]
