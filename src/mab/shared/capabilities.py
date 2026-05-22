from __future__ import annotations

from collections.abc import Iterable


def matches_capabilities(
    agent_capabilities: Iterable[str],
    required_all: Iterable[str],
    required_any: Iterable[str],
) -> bool:
    caps = set(agent_capabilities)
    required_all = list(required_all)
    required_any = list(required_any)
    if required_all and not all(tag in caps for tag in required_all):
        return False
    if required_any and not any(tag in caps for tag in required_any):
        return False
    return True


# Map a longest-prefix model identifier to (family, tier).
# Order matters: longer prefixes must come before shorter ones so e.g.
# "claude-opus-4-7" matches "claude-opus" before "claude" alone would.
_KNOWN_MODEL_PREFIXES: dict[str, tuple[str, str]] = {
    # Anthropic Claude (closed API)
    "claude-opus": ("claude", "opus"),
    "claude-sonnet": ("claude", "sonnet"),
    "claude-haiku": ("claude", "haiku"),
    # OpenAI hosted (closed API)
    "gpt-5": ("openai", "gpt-5"),
    "gpt-4o": ("openai", "gpt-4o"),
    "gpt-4": ("openai", "gpt-4"),
    # OpenAI open-source reasoning (typically via Ollama / vLLM)
    "gpt-oss": ("gpt-oss", "reasoning"),
    # Google
    "gemini-2.5-pro": ("google", "pro"),
    "gemini-2.5-flash-lite": ("google", "flash-lite"),
    "gemini-2.5-flash": ("google", "flash"),
    "gemma-3": ("google", "gemma-3"),
    "gemma": ("google", "gemma"),
    # Meta Llama
    "llama-4": ("meta", "llama-4"),
    "llama4": ("meta", "llama-4"),
    "llama-3.3": ("meta", "llama-3.3"),
    "llama3.3": ("meta", "llama-3.3"),
    "llama-3": ("meta", "llama-3"),
    "llama3": ("meta", "llama-3"),
    "llama": ("meta", "llama"),
    # Mistral
    "mistral-large": ("mistral", "large"),
    "mistral-small": ("mistral", "small"),
    "mistral": ("mistral", "mistral"),
    # DeepSeek
    "deepseek-r1": ("deepseek", "r1"),
    "deepseek-v3": ("deepseek", "v3"),
    "deepseek-coder": ("deepseek", "coder"),
    "deepseek": ("deepseek", "deepseek"),
    # Alibaba Qwen
    "qwen2.5-coder": ("alibaba", "qwen2.5-coder"),
    "qwen3": ("alibaba", "qwen3"),
    "qwen2.5": ("alibaba", "qwen2.5"),
    "qwen2": ("alibaba", "qwen2"),
    "qwen": ("alibaba", "qwen"),
    # Microsoft Phi
    "phi-4": ("microsoft", "phi-4"),
    "phi4": ("microsoft", "phi-4"),
    "phi": ("microsoft", "phi"),
}

# Auto-derived provider when we can be confident. Closed-API families only —
# OSS models (gpt-oss, meta, mistral, deepseek, alibaba, microsoft) intentionally
# have no default provider, since they could be hosted via Ollama / vLLM /
# Bedrock / OpenAI / their own SaaS. The Ollama-tag heuristic below covers the
# common case (`model:tag` form auto-sets provider:ollama).
_PROVIDER_BY_FAMILY: dict[str, str] = {
    "claude": "anthropic",
    "openai": "openai",
    "google": "google",
}


def derive_capabilities_from_model(model: str) -> list[str]:
    """Derive structured capability tags from a model identifier.

    Recognizes:
      - Anthropic Claude / OpenAI GPT / Google Gemini (closed APIs) — full
        family / tier / provider derivation
      - Popular open-source families (gpt-oss, llama, qwen, deepseek, mistral,
        gemma, phi) — family + tier; provider only via Ollama-tag heuristic
      - Ollama-style `model:tag` form — auto-adds `size:<tag>` and
        `provider:ollama` (Ollama uses `:tag` to pin variants like `:20b`,
        `:7b-instruct`, `:latest`)

    Unknown models drop back to just `model:<x>` (no family/tier guessing).
    """
    model = model.strip().lower()
    if not model:
        return []

    tags: list[str] = [f"model:{model}"]

    # Ollama-tag form (e.g. "gpt-oss:20b", "llama3.3:70b", "qwen2.5-coder:32b",
    # "gpt-oss:120b-cloud") — split off the size/variant and assume Ollama
    # hosting. `-cloud` suffix marks Ollama Cloud variants (vs. local GPU host).
    if ":" in model:
        name, raw_tag = model.split(":", 1)
        if raw_tag:
            if raw_tag.endswith("-cloud"):
                base_tag = raw_tag[:-len("-cloud")]
                if base_tag:
                    tags.append(f"size:{base_tag}")
                tags.append("host:cloud")
            else:
                tags.append(f"size:{raw_tag}")
                tags.append("host:local")
            tags.append("provider:ollama")
        match_key = name
    else:
        match_key = model

    for prefix, (family, tier) in _KNOWN_MODEL_PREFIXES.items():
        if match_key.startswith(prefix):
            tags.append(f"family:{family}")
            tags.append(f"tier:{tier}")
            if "provider:ollama" not in tags:
                provider = _PROVIDER_BY_FAMILY.get(family)
                if provider:
                    tags.append(f"provider:{provider}")
            break
    return tags
