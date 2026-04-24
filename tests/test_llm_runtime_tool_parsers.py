"""Tests for ``concinno.llm_runtime.tool_parsers``.

Covers:
  * ``GemmaToolCallParser`` recovery regex, dedup, cap
  * ``ToolCallParser`` Protocol structural match
  * ``get_parser`` dispatch rules (None / prefix / unknown)
  * ``register_parser`` extension hook
  * Legacy ``_extract_gemma_tool_calls`` / ``_strip_gemma_tool_calls``
    delegations in ``in_process`` still produce identical output.
"""

from __future__ import annotations

import json

import pytest

from concinno.llm_runtime.in_process import (
    DEFAULT_GEMMA_TOOL_CALL_CAP,
    _extract_gemma_tool_calls,
    _strip_gemma_tool_calls,
)
from concinno.llm_runtime.tool_parsers import (
    DEFAULT_TOOL_CALL_CAP,
    GemmaToolCallParser,
    ToolCallParser,
    get_parser,
    register_parser,
)


# ── Fixtures ────────────────────────────────────────────────────────

SINGLE_CALL = (
    '<|tool_call>call:python_exec'
    '{code:<|"|>round(x, 3)<|"|>}<tool_call|>'
)

DUAL_DISTINCT = (
    '<|tool_call>call:python_exec'
    '{code:<|"|>round(1.4564, 3)<|"|>}<tool_call|>'
    ' <|tool_call>call:read_attachment'
    '{path:<|"|>/tmp/x.pdb<|"|>}<tool_call|>'
)

DEDUP_ECHOES = (
    '<|tool_call>call:python_exec'
    '{code:<|"|>12 * 8<|"|>}<tool_call|>'
    ' thought narrates: <|tool_call>call:python_exec'
    '{code:<|"|>12 * 8<|"|>}<tool_call|>'
    ' again: <|tool_call>call:python_exec'
    '{code:<|"|>12 * 8<|"|>}<tool_call|>'
)


# ── GemmaToolCallParser behaviour ───────────────────────────────────


class TestGemmaToolCallParserShape:
    def test_family_attribute(self) -> None:
        assert GemmaToolCallParser().family == "gemma"

    def test_satisfies_protocol(self) -> None:
        assert isinstance(GemmaToolCallParser(), ToolCallParser)

    def test_parse_returns_tuple(self) -> None:
        calls, cleaned = GemmaToolCallParser().parse(SINGLE_CALL)
        assert isinstance(calls, list)
        # cleaned is str | None — here whole content is markers
        assert cleaned is None or isinstance(cleaned, str)


class TestGemmaSingleCall:
    def test_name_captured(self) -> None:
        calls, _ = GemmaToolCallParser().parse(SINGLE_CALL)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "python_exec"

    def test_argument_json_encoded(self) -> None:
        calls, _ = GemmaToolCallParser().parse(SINGLE_CALL)
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {"code": "round(x, 3)"}

    def test_openai_shape_fields(self) -> None:
        calls, _ = GemmaToolCallParser().parse(SINGLE_CALL)
        c = calls[0]
        assert c["type"] == "function"
        assert c["id"].startswith("call_gemma_")
        assert set(c["function"].keys()) == {"name", "arguments"}

    def test_stripping_removes_markers(self) -> None:
        _, cleaned = GemmaToolCallParser().parse(SINGLE_CALL)
        # Whole content is just the marker → cleaned is None
        assert cleaned is None


class TestGemmaMultipleCalls:
    def test_distinct_calls_both_returned(self) -> None:
        calls, _ = GemmaToolCallParser().parse(DUAL_DISTINCT)
        assert len(calls) == 2
        names = {c["function"]["name"] for c in calls}
        assert names == {"python_exec", "read_attachment"}

    def test_order_preserved(self) -> None:
        calls, _ = GemmaToolCallParser().parse(DUAL_DISTINCT)
        assert calls[0]["function"]["name"] == "python_exec"
        assert calls[1]["function"]["name"] == "read_attachment"

    def test_cleaned_retains_surrounding_text(self) -> None:
        content = f"prefix text {SINGLE_CALL} suffix text"
        _, cleaned = GemmaToolCallParser().parse(content)
        assert cleaned is not None
        assert "prefix text" in cleaned
        assert "suffix text" in cleaned
        # Marker itself is stripped
        assert "<|tool_call>" not in cleaned


class TestGemmaDedupAndCap:
    """Gemma 4 thinking-mode echo defence."""

    def test_identical_echoes_collapse_to_one(self) -> None:
        calls, _ = GemmaToolCallParser().parse(DEDUP_ECHOES)
        assert len(calls) == 1
        args = json.loads(calls[0]["function"]["arguments"])
        assert args == {"code": "12 * 8"}

    def test_dedup_by_signature_distinct_args_kept(self) -> None:
        content = (
            '<|tool_call>call:python_exec{code:<|"|>a<|"|>}<tool_call|>'
            '<|tool_call>call:python_exec{code:<|"|>b<|"|>}<tool_call|>'
            # Echo of the first → should be deduped
            '<|tool_call>call:python_exec{code:<|"|>a<|"|>}<tool_call|>'
        )
        calls, _ = GemmaToolCallParser().parse(content)
        assert len(calls) == 2

    def test_cap_default_is_three(self) -> None:
        assert DEFAULT_TOOL_CALL_CAP == 3

    def test_cap_limits_output(self) -> None:
        # Five distinct calls, cap=2 → only 2 returned.
        parts = [
            f'<|tool_call>call:f{i}{{x:<|"|>{i}<|"|>}}<tool_call|>'
            for i in range(5)
        ]
        calls, _ = GemmaToolCallParser().parse(
            "".join(parts), max_calls=2,
        )
        assert len(calls) == 2
        assert calls[0]["function"]["name"] == "f0"
        assert calls[1]["function"]["name"] == "f1"

    def test_cap_default_applies_when_not_overridden(self) -> None:
        parts = [
            f'<|tool_call>call:f{i}{{x:<|"|>{i}<|"|>}}<tool_call|>'
            for i in range(10)
        ]
        calls, _ = GemmaToolCallParser().parse("".join(parts))
        assert len(calls) == DEFAULT_TOOL_CALL_CAP


class TestGemmaEmpty:
    def test_no_markers_returns_empty(self) -> None:
        calls, cleaned = GemmaToolCallParser().parse(
            "plain assistant text, no tool calls here"
        )
        assert calls == []
        assert cleaned == "plain assistant text, no tool calls here"

    def test_empty_content_returns_empty(self) -> None:
        calls, cleaned = GemmaToolCallParser().parse("")
        assert calls == []
        assert cleaned is None


class TestGemmaShouldAttempt:
    def test_requires_tools_given(self) -> None:
        p = GemmaToolCallParser()
        assert p.should_attempt(
            content="anything",
            tools_given=[{"name": "x"}],
            native_tool_calls=None,
        ) is True

    def test_skips_when_no_tools(self) -> None:
        p = GemmaToolCallParser()
        assert p.should_attempt(
            content="anything",
            tools_given=None,
            native_tool_calls=None,
        ) is False

    def test_skips_when_tools_empty_list(self) -> None:
        p = GemmaToolCallParser()
        assert p.should_attempt(
            content="anything",
            tools_given=[],
            native_tool_calls=None,
        ) is False

    def test_skips_when_native_tool_calls_present(self) -> None:
        p = GemmaToolCallParser()
        assert p.should_attempt(
            content="anything",
            tools_given=[{"name": "x"}],
            native_tool_calls=[{"id": "abc"}],
        ) is False


# ── Registry dispatch ──────────────────────────────────────────────


class TestGetParserDispatch:
    def test_none_returns_gemma_for_backcompat(self) -> None:
        # Legacy path: chat_format=None + Gemma GGUF loaded (pod
        # default) must keep running Gemma recovery.
        assert isinstance(get_parser(None), GemmaToolCallParser)

    def test_exact_gemma_key(self) -> None:
        assert isinstance(get_parser("gemma"), GemmaToolCallParser)

    def test_gemma_prefix_match(self) -> None:
        for fmt in ("gemma-3", "gemma-4", "gemma-4-it", "gemma4"):
            assert isinstance(get_parser(fmt), GemmaToolCallParser), fmt

    def test_case_insensitive(self) -> None:
        for fmt in ("Gemma", "GEMMA-4-IT", "GeMmA"):
            assert isinstance(get_parser(fmt), GemmaToolCallParser), fmt

    def test_unknown_format_returns_none(self) -> None:
        # Llama / Mistral / functionary / chatml with native handlers
        # should NOT get post-parse recovery. They return None from the
        # registry and the caller keeps native tool_calls unchanged.
        for fmt in ("llama-3", "llama-3.1", "mistral", "functionary-v2",
                    "chatml", "phi-3"):
            assert get_parser(fmt) is None, fmt

    def test_empty_string_not_matched(self) -> None:
        # "" is falsy but not None — treat as unknown, return None
        # (do NOT fall back to Gemma for legacy None-only semantics).
        assert get_parser("") is None


class TestRegisterParserExtension:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        # Snapshot registry so per-test extensions don't leak.
        from concinno.llm_runtime.tool_parsers import _REGISTRY
        snapshot = dict(_REGISTRY)
        yield
        _REGISTRY.clear()
        _REGISTRY.update(snapshot)

    def test_register_adds_entry(self) -> None:
        class QwenParser:
            family = "qwen"
            def should_attempt(self, **_: object) -> bool:
                return False
            def parse(
                self, content: str, *, max_calls: int = 3,
            ) -> tuple[list, str | None]:
                return [], content

        register_parser("qwen", QwenParser)
        assert isinstance(get_parser("qwen-2.5-coder"), QwenParser)

    def test_register_is_case_insensitive(self) -> None:
        class UpperParser:
            family = "UPPER"
            def should_attempt(self, **_: object) -> bool:
                return False
            def parse(
                self, content: str, *, max_calls: int = 3,
            ) -> tuple[list, str | None]:
                return [], content

        register_parser("UPPER", UpperParser)
        assert isinstance(get_parser("upper-variant"), UpperParser)


# ── Legacy delegation parity ───────────────────────────────────────
#
# The 2.21.0-rc internal helpers ``_extract_gemma_tool_calls`` /
# ``_strip_gemma_tool_calls`` are now thin wrappers over the Parser.
# Verify their output is byte-identical to the new API so any caller
# that imported them directly keeps working.


class TestLegacyDelegationParity:
    def test_extract_matches_parser(self) -> None:
        legacy = _extract_gemma_tool_calls(DUAL_DISTINCT)
        direct, _ = GemmaToolCallParser().parse(DUAL_DISTINCT)
        assert legacy == direct

    def test_strip_matches_parser(self) -> None:
        legacy_stripped = _strip_gemma_tool_calls(
            f"prefix {SINGLE_CALL} suffix"
        )
        _, direct_cleaned = GemmaToolCallParser().parse(
            f"prefix {SINGLE_CALL} suffix"
        )
        assert legacy_stripped == direct_cleaned

    def test_default_cap_alias(self) -> None:
        assert DEFAULT_GEMMA_TOOL_CALL_CAP == DEFAULT_TOOL_CALL_CAP

    def test_legacy_cap_kwarg_still_works(self) -> None:
        parts = [
            f'<|tool_call>call:f{i}{{x:<|"|>{i}<|"|>}}<tool_call|>'
            for i in range(5)
        ]
        out = _extract_gemma_tool_calls("".join(parts), max_calls=2)
        assert len(out) == 2
