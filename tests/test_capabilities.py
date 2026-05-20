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
