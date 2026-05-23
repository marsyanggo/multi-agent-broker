"""CLI entry point for the standalone worker daemon (`mab-worker`).

Wires CLI args + env vars into an adapter instance + WorkerDaemon and
runs it under signal-handled asyncio. Each adapter has its own flag
namespace so misconfiguration is caught at startup rather than mid-task.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from mab.worker.adapters.anthropic import AnthropicAdapter
from mab.worker.adapters.base import LLMAdapter
from mab.worker.adapters.claude_cli import ClaudeCLIAdapter
from mab.worker.adapters.mock import MockAdapter
from mab.worker.adapters.ollama import OllamaAdapter
from mab.worker.daemon import WorkerDaemon, install_signal_handlers

log = logging.getLogger("mab.worker")


ADAPTER_CHOICES = ("anthropic", "ollama", "claude-cli", "mock")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mab-worker",
        description=(
            "Standalone worker daemon for mab-broker. Picks up tasks from "
            "the broker via wait_for_task push, runs them through an LLM "
            "adapter, reports back. Designed to run as a long-lived systemd "
            "user service — no GUI, no interactive prompts."
        ),
    )

    # --- broker connection ---
    p.add_argument(
        "--broker-url",
        default=os.environ.get("MAB_BROKER_URL", "http://localhost:8420"),
        help="broker REST URL (default $MAB_BROKER_URL or http://localhost:8420)",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("MAB_API_KEY"),
        help="API key for this worker's agent (default $MAB_API_KEY)",
    )

    # --- identity ---
    p.add_argument(
        "--model",
        default=os.environ.get("MAB_MODEL"),
        help=(
            "model identifier to declare on startup (e.g. claude-sonnet-4-6, "
            "gpt-oss:120b-cloud, llama3.3:70b). Auto-derives capability tags. "
            "Required."
        ),
    )
    p.add_argument(
        "--capabilities",
        default=os.environ.get("MAB_CAPABILITIES", ""),
        help="extra capability tags (comma-separated) layered on --model derivation",
    )

    # --- adapter selection ---
    p.add_argument(
        "--adapter",
        choices=ADAPTER_CHOICES,
        default=os.environ.get("MAB_ADAPTER"),
        help="LLM backend; required",
    )

    # --- daemon tuning ---
    p.add_argument(
        "--per-task-timeout",
        type=float,
        default=float(os.environ.get("MAB_PER_TASK_TIMEOUT", "600")),
        help="hard timeout per task in seconds (default 600)",
    )
    p.add_argument(
        "--wait-for-task-timeout",
        type=float,
        default=float(os.environ.get("MAB_WAIT_FOR_TASK_TIMEOUT", "60")),
        help="wait_for_task block timeout in seconds before re-loop (default 60)",
    )
    p.add_argument(
        "--log-level",
        default=os.environ.get("MAB_LOG_LEVEL", "INFO"),
        help="Python logging level (default INFO)",
    )

    # --- Anthropic adapter ---
    g = p.add_argument_group("anthropic adapter")
    g.add_argument(
        "--anthropic-api-key",
        default=os.environ.get("ANTHROPIC_API_KEY"),
        help="Anthropic API key (default $ANTHROPIC_API_KEY)",
    )
    g.add_argument(
        "--anthropic-base-url",
        default=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )
    g.add_argument("--anthropic-max-tokens", type=int, default=1024)
    g.add_argument("--anthropic-temperature", type=float, default=None)

    # --- Ollama adapter ---
    g = p.add_argument_group("ollama adapter")
    g.add_argument(
        "--ollama-base-url",
        default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        help=(
            "Ollama endpoint (default $OLLAMA_BASE_URL or "
            "http://localhost:11434 for local). Use https://ollama.com for Cloud."
        ),
    )
    g.add_argument(
        "--ollama-api-key",
        default=os.environ.get("OLLAMA_API_KEY"),
        help="Ollama Cloud auth key (default $OLLAMA_API_KEY)",
    )

    # --- shared adapter knobs ---
    g = p.add_argument_group("LLM behaviour (anthropic / ollama)")
    g.add_argument(
        "--system-prompt",
        default=os.environ.get("MAB_SYSTEM_PROMPT"),
        help="system prompt prepended to each task (for anthropic and ollama adapters)",
    )

    # --- Claude CLI adapter ---
    g = p.add_argument_group("claude-cli adapter")
    g.add_argument(
        "--claude-bin",
        default=os.environ.get("CLAUDE_BIN", "claude"),
        help="path to the claude CLI binary (default $CLAUDE_BIN or `claude`)",
    )
    g.add_argument(
        "--claude-prompt-template",
        default=os.environ.get(
            "MAB_CLAUDE_PROMPT_TEMPLATE", "{description}"
        ),
        help="template substituted with {description},{title},{id} per task",
    )
    g.add_argument(
        "--no-skip-permissions",
        action="store_true",
        help="DON'T pass --dangerously-skip-permissions to claude -p (off by default)",
    )

    # --- mock adapter ---
    g = p.add_argument_group("mock adapter")
    g.add_argument(
        "--mock-response",
        default=os.environ.get("MAB_MOCK_RESPONSE"),
        help="fixed string the mock adapter returns (default echoes description)",
    )

    return p


def parse_capabilities(spec: str) -> list[str]:
    return [c.strip() for c in spec.split(",") if c.strip()]


def build_adapter(args: argparse.Namespace) -> LLMAdapter:
    if args.adapter == "anthropic":
        return AnthropicAdapter(
            model=args.model,
            api_key=args.anthropic_api_key,
            base_url=args.anthropic_base_url,
            max_tokens=args.anthropic_max_tokens,
            temperature=args.anthropic_temperature,
            system_prompt=args.system_prompt,
        )
    if args.adapter == "ollama":
        return OllamaAdapter(
            model=args.model,
            base_url=args.ollama_base_url,
            api_key=args.ollama_api_key,
            system_prompt=args.system_prompt,
        )
    if args.adapter == "claude-cli":
        return ClaudeCLIAdapter(
            model=args.model,
            claude_bin=args.claude_bin,
            prompt_template=args.claude_prompt_template,
            dangerously_skip_permissions=not args.no_skip_permissions,
        )
    if args.adapter == "mock":
        return MockAdapter(response=args.mock_response)
    raise ValueError(f"unknown adapter: {args.adapter}")


def validate(args: argparse.Namespace) -> None:
    errs: list[str] = []
    if not args.api_key:
        errs.append("--api-key (or MAB_API_KEY env) required")
    if not args.model:
        errs.append("--model (or MAB_MODEL env) required")
    if not args.adapter:
        errs.append(f"--adapter (or MAB_ADAPTER env) required; one of {ADAPTER_CHOICES}")
    if errs:
        print("error: " + "; ".join(errs), file=sys.stderr)
        sys.exit(2)


async def run(args: argparse.Namespace) -> None:
    adapter = build_adapter(args)
    daemon = WorkerDaemon(
        broker_url=args.broker_url,
        api_key=args.api_key,
        adapter=adapter,
        model=args.model,
        extra_capabilities=parse_capabilities(args.capabilities) or None,
        per_task_timeout=args.per_task_timeout,
        wait_for_task_timeout=args.wait_for_task_timeout,
    )
    install_signal_handlers(daemon)
    await daemon.start()


def main() -> None:
    args = build_parser().parse_args()
    validate(args)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    )

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
