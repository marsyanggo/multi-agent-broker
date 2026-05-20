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


_KNOWN_MODEL_PREFIXES: dict[str, tuple[str, str]] = {
    "claude-opus": ("claude", "opus"),
    "claude-sonnet": ("claude", "sonnet"),
    "claude-haiku": ("claude", "haiku"),
    "gpt-4o": ("openai", "gpt-4o"),
    "gpt-4": ("openai", "gpt-4"),
    "gpt-5": ("openai", "gpt-5"),
    "gemini-2.5-pro": ("google", "pro"),
    "gemini-2.5-flash": ("google", "flash"),
    "gemini-flash-lite": ("google", "flash-lite"),
    "llama": ("meta", "llama"),
    "mistral": ("mistral", "mistral"),
}

_PROVIDER_BY_FAMILY: dict[str, str] = {
    "claude": "anthropic",
    "openai": "openai",
    "google": "google",
    "meta": "meta",
    "mistral": "mistral",
}


def derive_capabilities_from_model(model: str) -> list[str]:
    model = model.strip().lower()
    if not model:
        return []
    tags: list[str] = [f"model:{model}"]
    for prefix, (family, tier) in _KNOWN_MODEL_PREFIXES.items():
        if model.startswith(prefix):
            tags.append(f"family:{family}")
            tags.append(f"tier:{tier}")
            provider = _PROVIDER_BY_FAMILY.get(family)
            if provider:
                tags.append(f"provider:{provider}")
            break
    return tags
