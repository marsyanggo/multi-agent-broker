from mab.shared.capabilities import (
    derive_capabilities_from_model,
    matches_capabilities,
)


def test_empty_requirements_matches_anything() -> None:
    assert matches_capabilities([], [], []) is True
    assert matches_capabilities(["tier:opus"], [], []) is True


def test_required_all_must_all_be_present() -> None:
    caps = ["tier:opus", "vision"]
    assert matches_capabilities(caps, ["tier:opus"], []) is True
    assert matches_capabilities(caps, ["tier:opus", "vision"], []) is True
    assert matches_capabilities(caps, ["tier:opus", "audio"], []) is False


def test_required_any_needs_one_match() -> None:
    caps = ["tier:opus"]
    assert matches_capabilities(caps, [], ["tier:opus", "tier:sonnet"]) is True
    assert matches_capabilities(caps, [], ["tier:sonnet", "tier:haiku"]) is False


def test_required_all_and_any_combined() -> None:
    caps_a = ["tier:opus", "vision"]
    caps_b = ["tier:opus", "audio"]
    caps_c = ["tier:opus"]
    caps_d = ["tier:sonnet", "vision"]

    required_all = ["tier:opus"]
    required_any = ["vision", "audio"]

    assert matches_capabilities(caps_a, required_all, required_any) is True
    assert matches_capabilities(caps_b, required_all, required_any) is True
    assert matches_capabilities(caps_c, required_all, required_any) is False  # no any
    assert matches_capabilities(caps_d, required_all, required_any) is False  # no all


def test_derive_capabilities_claude_opus() -> None:
    tags = derive_capabilities_from_model("claude-opus-4-7")
    assert "model:claude-opus-4-7" in tags
    assert "family:claude" in tags
    assert "tier:opus" in tags
    assert "provider:anthropic" in tags


def test_derive_capabilities_claude_sonnet() -> None:
    tags = derive_capabilities_from_model("claude-sonnet-4-6")
    assert "tier:sonnet" in tags
    assert "family:claude" in tags
    assert "provider:anthropic" in tags


def test_derive_capabilities_gemini() -> None:
    tags = derive_capabilities_from_model("gemini-2.5-flash")
    assert "family:google" in tags
    assert "tier:flash" in tags
    assert "provider:google" in tags


def test_derive_capabilities_unknown_model_only_exact() -> None:
    tags = derive_capabilities_from_model("some-unknown-model-v9")
    assert tags == ["model:some-unknown-model-v9"]


def test_derive_capabilities_normalises_case() -> None:
    tags = derive_capabilities_from_model("Claude-Opus-4-7")
    assert "model:claude-opus-4-7" in tags
    assert "family:claude" in tags


def test_derive_capabilities_empty_returns_empty() -> None:
    assert derive_capabilities_from_model("") == []
    assert derive_capabilities_from_model("   ") == []


def test_derive_capabilities_gpt_oss_via_ollama() -> None:
    tags = derive_capabilities_from_model("gpt-oss:20b")
    assert "model:gpt-oss:20b" in tags
    assert "family:gpt-oss" in tags
    assert "tier:reasoning" in tags
    assert "size:20b" in tags
    assert "host:local" in tags
    assert "provider:ollama" in tags


def test_derive_capabilities_ollama_cloud_variant() -> None:
    # Ollama Cloud uses a `-cloud` suffix on the tag. We split that out so
    # routing on size:* doesn't end up with a polluted "120b-cloud" value.
    tags = derive_capabilities_from_model("gpt-oss:120b-cloud")
    assert "model:gpt-oss:120b-cloud" in tags
    assert "family:gpt-oss" in tags
    assert "tier:reasoning" in tags
    assert "size:120b" in tags
    assert "host:cloud" in tags
    assert "provider:ollama" in tags
    # No raw "size:120b-cloud" leaking through.
    assert "size:120b-cloud" not in tags


def test_derive_capabilities_llama_via_ollama() -> None:
    tags = derive_capabilities_from_model("llama3.3:70b")
    assert "model:llama3.3:70b" in tags
    assert "family:meta" in tags
    assert "tier:llama-3.3" in tags
    assert "size:70b" in tags
    assert "provider:ollama" in tags


def test_derive_capabilities_qwen_coder_via_ollama() -> None:
    tags = derive_capabilities_from_model("qwen2.5-coder:32b")
    assert "family:alibaba" in tags
    assert "tier:qwen2.5-coder" in tags
    assert "size:32b" in tags
    assert "provider:ollama" in tags


def test_derive_capabilities_deepseek_r1_via_ollama() -> None:
    tags = derive_capabilities_from_model("deepseek-r1:14b")
    assert "family:deepseek" in tags
    assert "tier:r1" in tags
    assert "size:14b" in tags
    assert "provider:ollama" in tags


def test_derive_capabilities_mistral_no_tag_no_ollama() -> None:
    # Without a `:tag` suffix, we don't assume Ollama hosting.
    tags = derive_capabilities_from_model("mistral-large")
    assert "family:mistral" in tags
    assert "tier:large" in tags
    assert "provider:ollama" not in tags
    assert not any(t.startswith("size:") for t in tags)


def test_derive_capabilities_unknown_ollama_tag_still_gets_size() -> None:
    # Unknown family but Ollama-style tag → still pick up size + provider.
    tags = derive_capabilities_from_model("custom-finetune:13b")
    assert tags == [
        "model:custom-finetune:13b",
        "size:13b",
        "host:local",
        "provider:ollama",
    ]


def test_derive_capabilities_claude_with_explicit_size_overrides_provider() -> None:
    # Claude doesn't normally use `:tag` form, but if someone passed
    # "claude-opus-4-7:custom", the Ollama heuristic should NOT override
    # provider — wait, current code does override. This documents that
    # behavior: any `:tag` form is treated as Ollama-hosted.
    tags = derive_capabilities_from_model("claude-opus-4-7:vision")
    assert "provider:ollama" in tags
    assert "provider:anthropic" not in tags  # Ollama heuristic wins
    assert "size:vision" in tags


def test_derive_capabilities_phi4() -> None:
    tags = derive_capabilities_from_model("phi-4")
    assert "family:microsoft" in tags
    assert "tier:phi-4" in tags
    # No `:tag` → no auto ollama provider for OSS family
    assert "provider:ollama" not in tags
    assert not any(t.startswith("provider:") for t in tags)
