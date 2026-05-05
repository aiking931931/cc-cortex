"""Tests for concinno.cbua_presets — V4 Think Max preamble + composer."""

from __future__ import annotations

import pytest

from concinno.cbua_presets import (
    PRESETS,
    THINK_MAX_PROMPT,
    build_chaotic_directives,
)

# ── THINK_MAX_PROMPT verbatim properties ─────────────────────


def test_think_max_prompt_is_string_and_nonempty() -> None:
    assert isinstance(THINK_MAX_PROMPT, str)
    assert len(THINK_MAX_PROMPT) > 100


def test_think_max_prompt_signature_phrases() -> None:
    """Verbatim from V4 §5.1.1 Table 3 — these phrases must be present."""
    must_have = [
        "Reasoning Effort: Absolute maximum",
        "no shortcuts permitted",
        "rigorously stress-testing",
        "edge cases, and adversarial scenarios",
        "every intermediate step",
        "rejected hypothesis",
        "no assumption is left unchecked",
    ]
    for phrase in must_have:
        assert phrase in THINK_MAX_PROMPT, f"missing verbatim phrase: {phrase!r}"


def test_think_max_prompt_no_trailing_whitespace() -> None:
    assert THINK_MAX_PROMPT == THINK_MAX_PROMPT.rstrip()


# ── build_chaotic_directives ─────────────────────────────────


def test_build_chaotic_directives_default_includes_think_max() -> None:
    out = build_chaotic_directives()
    assert THINK_MAX_PROMPT in out


def test_build_chaotic_directives_default_includes_concinno_cognition() -> None:
    """Default appends L0/L1/L2 imperative directives from cognitive_inject."""
    out = build_chaotic_directives()
    # L0 / L1 / L2 signature phrases (from cognitive_inject._L0/_L1/_L2).
    assert "Fix all errors you see now" in out  # _L0_HARD_RULES
    assert "First instinct" in out  # _L1_ANTI_BIAS
    assert "Sweet spot" in out  # _L2_COGNITION


def test_build_chaotic_directives_without_concinno_cognition() -> None:
    out = build_chaotic_directives(include_concinno_cognition=False)
    assert out == THINK_MAX_PROMPT


def test_build_chaotic_directives_extra_directives_appended() -> None:
    extra = "- Output language: zh-TW."
    out = build_chaotic_directives(extra_directives=extra)
    assert out.endswith(extra)


def test_build_chaotic_directives_separator_is_blank_line() -> None:
    out = build_chaotic_directives()
    # Sections separated by exactly one blank line ("\n\n").
    sections = out.split("\n\n")
    assert len(sections) >= 2
    # Each section is non-empty.
    for section in sections:
        assert section.strip()


# ── Preset registry ──────────────────────────────────────────


def test_preset_registry_has_chaotic_v4() -> None:
    assert "chaotic_v4_think_max" in PRESETS


def test_preset_registry_entries_have_required_metadata() -> None:
    for name, entry in PRESETS.items():
        for key in ("name", "source", "verbatim", "posterior_tier",
                    "constant_name", "use_when"):
            assert key in entry, f"{name} missing {key}"


def test_preset_registry_chaotic_v4_metadata() -> None:
    entry = PRESETS["chaotic_v4_think_max"]
    assert entry["constant_name"] == "THINK_MAX_PROMPT"
    assert entry["verbatim"] == "true"
    assert entry["posterior_tier"] == "HIGH"
    assert "DeepSeek-V4" in entry["source"]
    # Source must include path to the cached PDF in the repo, NOT a blog.
    assert "DeepSeek_V4.pdf" in entry["source"]


# ── Provenance / anti-hallucination guard ────────────────────


@pytest.mark.parametrize("forbidden", ["deepwiki", "Medium", "36kr"])
def test_module_docstring_does_not_cite_secondary_blogs(forbidden: str) -> None:
    """Per V4 audit MEMORY #4j: only cite primary sources."""
    import concinno.cbua_presets as mod

    text = (mod.__doc__ or "") + str(mod.PRESETS)
    assert forbidden not in text, (
        f"primary-source rule violated: {forbidden!r} cited in module text"
    )
