"""Tests for cc_cortex.cache.cache_break_detector."""

from __future__ import annotations

from typing import Any

import pytest

from cc_cortex.cache.cache_break_detector import (
    BreakReport,
    CacheBreakDetector,
    PreviousState,
    hash_field,
    hash_per_tool,
)

# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def system_text() -> str:
    return "You are a helpful assistant. Always respond concisely."


@pytest.fixture
def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "Read",
            "description": "Read a file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "Edit",
            "description": "Edit a file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "AgentTool",
            "description": "Spawn sub-agent.",
            "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
        },
    ]


@pytest.fixture
def detector(tmp_path: Any) -> CacheBreakDetector:
    return CacheBreakDetector(cache_dir=str(tmp_path), session_id="test_sess")


# ── hash_field ──────────────────────────────────────────────────────


def test_hash_field_string_stable() -> None:
    h1 = hash_field("hello world")
    h2 = hash_field("hello world")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hexdigest
    assert hash_field("hello world") != hash_field("hello world!")


def test_hash_field_dict_order_invariant() -> None:
    a = {"x": 1, "y": 2, "z": 3}
    b = {"z": 3, "y": 2, "x": 1}
    assert hash_field(a) == hash_field(b)
    # Nested dicts also order-invariant
    c = {"outer": {"k2": "v2", "k1": "v1"}}
    d = {"outer": {"k1": "v1", "k2": "v2"}}
    assert hash_field(c) == hash_field(d)


def test_hash_field_none_deterministic() -> None:
    h_none = hash_field(None)
    # Same digest every call
    assert h_none == hash_field(None)
    # Must not collide with the empty string (None vs "")
    assert h_none != hash_field("")
    # Must not collide with the string "null" either — None uses bytes b"null"
    # but "null" is UTF-8 encoded the same way, so these actually DO match.
    # The contract only requires determinism, not collision-freedom with
    # the literal string "null" (callers shouldn't pass that anyway).
    assert h_none == hash_field("null")


# ── hash_per_tool ───────────────────────────────────────────────────


def test_hash_per_tool_preserves_names(tools: list[dict[str, Any]]) -> None:
    result = hash_per_tool(tools)
    assert set(result.keys()) == {"Read", "Edit", "AgentTool"}
    for name, digest in result.items():
        assert len(digest) == 64
        assert isinstance(name, str)


def test_hash_per_tool_missing_name_fallback() -> None:
    malformed = [
        {"name": "Good", "description": "ok"},
        {"description": "no name here"},  # falls back to tool_1
        {"name": "", "description": "empty name"},  # empty also falls back
    ]
    result = hash_per_tool(malformed)
    assert "Good" in result
    assert "tool_1" in result
    assert "tool_2" in result
    assert len(result) == 3


# ── diff semantics ──────────────────────────────────────────────────


def test_first_request_reports_first_request(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    report = detector.detect(system=system_text, tools=tools)
    assert report.reasons == ("first_request",)
    assert report.details == {}
    assert report.changed_tool is None


def test_no_change_reports_no_break(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(system=system_text, tools=tools)
    report = detector.detect(system=system_text, tools=tools)
    assert report.reasons == ("no_break",)
    assert report.details == {}


def test_system_change_detected(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(system=system_text, tools=tools)
    report = detector.detect(system=system_text + " Extra.", tools=tools)
    assert "system_changed" in report.reasons
    assert "system_hash" in report.details
    old, new = report.details["system_hash"]
    assert old != new


def test_tools_change_detected(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(system=system_text, tools=tools)
    # Add a new tool — aggregate hash must differ
    new_tools = [*tools, {"name": "Grep", "description": "Search."}]
    report = detector.detect(system=system_text, tools=new_tools)
    assert "tools_changed" in report.reasons
    assert "per_tool_changed" in report.reasons


def test_single_tool_change_reports_changed_tool_name(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(system=system_text, tools=tools)
    mutated = [dict(t) for t in tools]
    mutated[2]["description"] = "Spawn sub-agent with NEW embed list."
    report = detector.detect(system=system_text, tools=mutated)
    assert "per_tool_changed" in report.reasons
    assert report.changed_tool == "AgentTool"


def test_multiple_tool_changes_reports_multiple(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(system=system_text, tools=tools)
    mutated = [dict(t) for t in tools]
    mutated[0]["description"] = "Read changed."
    mutated[1]["description"] = "Edit changed."
    report = detector.detect(system=system_text, tools=mutated)
    assert "per_tool_changed" in report.reasons
    assert report.changed_tool == "multiple"


def test_betas_change_detected(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(
        system=system_text,
        tools=tools,
        betas=("prompt-caching-2024-07-31",),
    )
    report = detector.detect(
        system=system_text,
        tools=tools,
        betas=("prompt-caching-2024-07-31", "extended-thinking-2025-01-01"),
    )
    assert "betas_changed" in report.reasons


def test_effort_change_detected(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(system=system_text, tools=tools, effort="medium")
    report = detector.detect(system=system_text, tools=tools, effort="high")
    assert "effort_changed" in report.reasons
    assert report.details["effort_value"] == ("medium", "high")


def test_strategy_change_detected(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(system=system_text, tools=tools, strategy="tool_based")
    report = detector.detect(system=system_text, tools=tools, strategy="system_prompt")
    assert "strategy_changed" in report.reasons
    assert report.details["global_cache_strategy"] == ("tool_based", "system_prompt")


# ── purity / mutation ───────────────────────────────────────────────


def test_snapshot_does_not_mutate_state(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    before = detector._previous
    assert before.is_empty()
    _ = detector.snapshot(system=system_text, tools=tools)
    # Still empty — snapshot is pure
    assert detector._previous.is_empty()


def test_diff_does_not_mutate_state(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    snap = detector.snapshot(system=system_text, tools=tools)
    _ = detector.diff(snap)
    assert detector._previous.is_empty()
    # Run twice — still empty
    _ = detector.diff(snap)
    assert detector._previous.is_empty()


def test_detect_commits_after_diff(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(system=system_text, tools=tools)
    assert not detector._previous.is_empty()
    assert detector._previous.system_hash == hash_field(system_text)


def test_stats_increment_per_detect(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(system=system_text, tools=tools)  # first_request
    detector.detect(system=system_text, tools=tools)  # no_break
    detector.detect(system=system_text + " x", tools=tools)  # system_changed
    stats = detector.stats()
    assert stats.get("first_request", 0) == 1
    assert stats.get("no_break", 0) == 1
    assert stats.get("system_changed", 0) == 1
    # stats() returns a copy — mutating it must not affect the detector
    stats["first_request"] = 999
    assert detector.stats().get("first_request") == 1


# ── persistence ─────────────────────────────────────────────────────


def test_save_load_roundtrip(
    tmp_path: Any,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    d1 = CacheBreakDetector(cache_dir=str(tmp_path), session_id="sess_a")
    d1.detect(system=system_text, tools=tools, betas=("beta-1",), effort="high")
    d1.save()

    # Fresh detector auto-loads on construction
    d2 = CacheBreakDetector(cache_dir=str(tmp_path), session_id="sess_a")
    assert not d2._previous.is_empty()
    assert d2._previous.system_hash == hash_field(system_text)
    assert d2._previous.betas == ("beta-1",)
    assert d2._previous.effort_value == "high"

    # Same input → no_break, proving state carried over
    report = d2.detect(
        system=system_text, tools=tools, betas=("beta-1",), effort="high"
    )
    assert report.reasons == ("no_break",)


# ── multi-field concurrent change ───────────────────────────────────


def test_concurrent_field_changes_report_all_reasons(
    detector: CacheBreakDetector,
    system_text: str,
    tools: list[dict[str, Any]],
) -> None:
    detector.detect(
        system=system_text,
        tools=tools,
        betas=("beta-a",),
        effort="low",
        strategy="none",
    )
    # Change every tracked field in one go
    mutated_tools = [dict(t) for t in tools]
    mutated_tools[0]["description"] = "Read with embed drift."
    report = detector.detect(
        system=system_text + " More.",
        tools=mutated_tools,
        betas=("beta-a", "beta-b"),
        effort="high",
        strategy="tool_based",
    )
    expected = {
        "system_changed",
        "tools_changed",
        "per_tool_changed",
        "betas_changed",
        "effort_changed",
        "strategy_changed",
    }
    assert expected.issubset(set(report.reasons))
    # Single-tool change → changed_tool set to that name
    assert report.changed_tool == "Read"


# ── misc hygiene ────────────────────────────────────────────────────


def test_previous_state_to_from_dict_roundtrip() -> None:
    original = PreviousState(
        system_hash="a" * 64,
        tools_hash="b" * 64,
        per_tool_hashes={"Read": "c" * 64},
        betas=("beta-x", "beta-y"),
        effort_value="medium",
        global_cache_strategy="tool_based",
    )
    data = original.to_dict()
    assert isinstance(data["betas"], list)  # JSON-safe
    restored = PreviousState.from_dict(data)
    assert restored == original


def test_in_memory_detector_has_no_store() -> None:
    d = CacheBreakDetector()  # no cache_dir
    d.save()  # no-op, must not raise
    d.load()  # no-op, must not raise
    report = d.detect(system="hi", tools=[])
    assert report.reasons == ("first_request",)


def test_break_report_is_a_dataclass() -> None:
    r = BreakReport(reasons=("no_break",), details={})
    assert r.reasons == ("no_break",)
    assert r.details == {}
    assert r.changed_tool is None
