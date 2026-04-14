"""Tests for cc_cortex.cache.autocompact."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from cc_cortex.cache.autocompact import (
    AUTOCOMPACT_BUFFER_TOKENS,
    DEFAULT_MODEL_BUDGETS,
    ERROR_THRESHOLD_BUFFER_TOKENS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    RECURSION_GUARDED_SOURCES,
    WARNING_THRESHOLD_BUFFER_TOKENS,
    AutoCompactExhausted,
    AutoCompactor,
    AutoCompactState,
    CompactRequest,
    CompactResult,
    ContextCollapseActive,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeCompactSink:
    """Scripted sink for deterministic test flows."""

    def __init__(
        self,
        *,
        results: list[CompactResult] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.results = list(results or [])
        self.raise_exc = raise_exc
        self.calls: list[CompactRequest] = []

    def summarize(self, req: CompactRequest) -> CompactResult:
        self.calls.append(req)
        if self.raise_exc is not None:
            raise self.raise_exc
        if not self.results:
            return CompactResult(
                success=True, summary_tokens=500, reclaimed_tokens=50_000
            )
        return self.results.pop(0)


def _ok_result(reclaimed: int = 50_000) -> CompactResult:
    return CompactResult(
        success=True, summary_tokens=500, reclaimed_tokens=reclaimed
    )


def _fail_result(error: str = "prompt_too_long") -> CompactResult:
    return CompactResult(
        success=False, summary_tokens=0, reclaimed_tokens=0, error=error
    )


# ---------------------------------------------------------------------------
# 1. constants
# ---------------------------------------------------------------------------


def test_default_threshold_constants() -> None:
    assert AUTOCOMPACT_BUFFER_TOKENS == 13_000
    assert WARNING_THRESHOLD_BUFFER_TOKENS == 20_000
    assert ERROR_THRESHOLD_BUFFER_TOKENS == 20_000
    assert MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES == 3


# ---------------------------------------------------------------------------
# 2-4. model budget + threshold math
# ---------------------------------------------------------------------------


def test_model_budget_lookup_opus_4_6() -> None:
    ac = AutoCompactor(model="claude-opus-4-6")
    assert ac.model_budget == 1_000_000
    assert DEFAULT_MODEL_BUDGETS["claude-opus-4-6"] == 1_000_000
    assert DEFAULT_MODEL_BUDGETS["claude-sonnet-4-6"] == 1_000_000
    assert DEFAULT_MODEL_BUDGETS["claude-haiku-4-5"] == 200_000


def test_model_budget_unknown_falls_back_to_200k(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="cc_cortex.cache.autocompact"):
        ac = AutoCompactor(model="claude-martian-99")
    assert ac.model_budget == 200_000
    assert any("unknown model" in r.message for r in caplog.records)


def test_get_autocompact_threshold_math() -> None:
    ac = AutoCompactor(model="claude-opus-4-6")
    # 1_000_000 - 13_000 - 20_000
    assert ac.get_autocompact_threshold() == 967_000

    ac_custom = AutoCompactor(
        model_budget=500_000,
        buffer_tokens=10_000,
        warning_threshold=5_000,
    )
    assert ac_custom.get_autocompact_threshold() == 485_000


# ---------------------------------------------------------------------------
# 5-9. should_trigger
# ---------------------------------------------------------------------------


def test_should_trigger_noop_below_threshold() -> None:
    ac = AutoCompactor(model="claude-opus-4-6")
    assert ac.should_trigger(current_tokens=100_000) == "noop"
    assert ac.should_trigger(current_tokens=966_999) == "noop"


def test_should_trigger_warning_zone() -> None:
    ac = AutoCompactor(model="claude-opus-4-6")
    # Above 967_000, below 987_000
    assert ac.should_trigger(current_tokens=970_000) == "warning"
    assert ac.should_trigger(current_tokens=986_999) == "warning"


def test_should_trigger_error_zone() -> None:
    ac = AutoCompactor(model="claude-opus-4-6")
    # At or above 987_000 (= 1_000_000 - 13_000)
    assert ac.should_trigger(current_tokens=987_000) == "error"
    assert ac.should_trigger(current_tokens=999_999) == "error"


def test_should_trigger_recursion_guarded_source_noop() -> None:
    ac = AutoCompactor(model="claude-opus-4-6")
    for src in ("session_memory", "compact", "ctx_agent"):
        assert (
            ac.should_trigger(current_tokens=999_999, source=src)  # type: ignore[arg-type]
            == "noop"
        )


def test_should_trigger_context_collapse_active_noop() -> None:
    ac = AutoCompactor(
        model="claude-opus-4-6", context_collapse_active=True
    )
    assert ac.should_trigger(current_tokens=999_999) == "noop"
    assert ac.should_trigger(current_tokens=100_000) == "noop"


# ---------------------------------------------------------------------------
# 10-18. run
# ---------------------------------------------------------------------------


def test_run_noop_returns_none() -> None:
    sink = FakeCompactSink()
    ac = AutoCompactor(model="claude-opus-4-6", sink=sink)
    assert ac.run(current_tokens=100_000) is None
    assert sink.calls == []


def test_run_success_resets_consecutive_failures() -> None:
    sink = FakeCompactSink(results=[_ok_result()])
    ac = AutoCompactor(model="claude-opus-4-6", sink=sink)
    ac.state.consecutive_failures = 2  # prior failures
    result = ac.run(current_tokens=970_000)
    assert result is not None
    assert result.success is True
    assert ac.state.consecutive_failures == 0


def test_run_success_increments_total() -> None:
    sink = FakeCompactSink(
        results=[_ok_result(reclaimed=40_000), _ok_result(reclaimed=60_000)]
    )
    ac = AutoCompactor(model="claude-opus-4-6", sink=sink)
    ac.run(current_tokens=970_000)
    ac.run(current_tokens=970_000)
    assert ac.state.total_compactions == 2
    assert ac.state.total_reclaimed_tokens == 100_000


def test_run_sink_failure_increments_consecutive() -> None:
    sink = FakeCompactSink(results=[_fail_result("prompt_too_long")])
    ac = AutoCompactor(model="claude-opus-4-6", sink=sink)
    result = ac.run(current_tokens=970_000)
    assert result is not None
    assert result.success is False
    assert ac.state.consecutive_failures == 1
    assert ac.state.last_error == "prompt_too_long"


def test_run_circuit_opens_at_max_failures() -> None:
    sink = FakeCompactSink(
        results=[_fail_result("e1"), _fail_result("e2"), _fail_result("e3")]
    )
    ac = AutoCompactor(model="claude-opus-4-6", sink=sink)
    ac.run(current_tokens=970_000)  # failure #1
    ac.run(current_tokens=970_000)  # failure #2
    # failure #3 trips the circuit and raises
    with pytest.raises(AutoCompactExhausted):
        ac.run(current_tokens=970_000)
    assert ac.state.circuit_open is True
    assert ac.state.consecutive_failures == 3


def test_run_circuit_open_raises_exhausted() -> None:
    sink = FakeCompactSink()
    ac = AutoCompactor(model="claude-opus-4-6", sink=sink)
    ac.state.circuit_open = True
    ac.state.consecutive_failures = 3
    ac.state.last_error = "prior_fail"
    with pytest.raises(AutoCompactExhausted, match="circuit open"):
        ac.run(current_tokens=970_000)
    assert sink.calls == []


def test_run_recursion_guard_beats_circuit_check() -> None:
    """A forked agent must get noop, not an exception, even when the
    circuit is open. Otherwise the fork bubbles the failure into the
    summarization pipeline it's supposed to be supporting."""
    sink = FakeCompactSink()
    ac = AutoCompactor(model="claude-opus-4-6", sink=sink)
    ac.state.circuit_open = True
    ac.state.consecutive_failures = 3
    # session_memory is guarded: must return None, not raise
    result = ac.run(current_tokens=999_999, source="session_memory")
    assert result is None
    assert sink.calls == []


def test_run_context_collapse_raises_active() -> None:
    sink = FakeCompactSink()
    ac = AutoCompactor(
        model="claude-opus-4-6",
        context_collapse_active=True,
        sink=sink,
    )
    with pytest.raises(ContextCollapseActive):
        ac.run(current_tokens=970_000)
    assert sink.calls == []


def test_run_no_sink_raises_runtime_error() -> None:
    ac = AutoCompactor(model="claude-opus-4-6", sink=None)
    with pytest.raises(RuntimeError, match="no sink configured"):
        ac.run(current_tokens=970_000)


# ---------------------------------------------------------------------------
# 19. persistence
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    cache_dir = str(tmp_path)
    sink = FakeCompactSink(results=[_ok_result(reclaimed=12_345)])
    ac = AutoCompactor(
        cache_dir=cache_dir,
        session_id="abcdef12-xyz",
        model="claude-opus-4-6",
        sink=sink,
    )
    ac.run(current_tokens=970_000)
    assert ac.state.total_compactions == 1
    assert ac.state.total_reclaimed_tokens == 12_345

    ac2 = AutoCompactor(
        cache_dir=cache_dir,
        session_id="abcdef12-xyz",
        model="claude-opus-4-6",
    )
    ac2.load()
    assert ac2.state.total_compactions == 1
    assert ac2.state.total_reclaimed_tokens == 12_345
    assert ac2.state.consecutive_failures == 0
    assert ac2.state.circuit_open is False


def test_save_load_preserves_circuit_open(tmp_path: Path) -> None:
    cache_dir = str(tmp_path)
    sink = FakeCompactSink(
        results=[_fail_result("e1"), _fail_result("e2"), _fail_result("e3")]
    )
    ac = AutoCompactor(
        cache_dir=cache_dir,
        session_id="xyz12345",
        model="claude-opus-4-6",
        sink=sink,
    )
    ac.run(current_tokens=970_000)
    ac.run(current_tokens=970_000)
    with pytest.raises(AutoCompactExhausted):
        ac.run(current_tokens=970_000)

    ac2 = AutoCompactor(
        cache_dir=cache_dir,
        session_id="xyz12345",
        model="claude-opus-4-6",
        sink=FakeCompactSink(),
    )
    ac2.load()
    assert ac2.state.circuit_open is True
    with pytest.raises(AutoCompactExhausted):
        ac2.run(current_tokens=970_000)


# ---------------------------------------------------------------------------
# 20. stats
# ---------------------------------------------------------------------------


def test_stats_tracks_counters() -> None:
    sink = FakeCompactSink(
        results=[_ok_result(reclaimed=30_000), _fail_result("e1")]
    )
    ac = AutoCompactor(model="claude-opus-4-6", sink=sink)
    ac.run(current_tokens=970_000)
    ac.run(current_tokens=970_000)
    stats = ac.stats()
    assert stats["total_compactions"] == 1
    assert stats["total_reclaimed_tokens"] == 30_000
    assert stats["consecutive_failures"] == 1
    assert stats["circuit_open"] is False
    assert stats["model_budget"] == 1_000_000
    assert stats["threshold"] == 967_000


# ---------------------------------------------------------------------------
# 21-22. structural invariants
# ---------------------------------------------------------------------------


def test_guarded_sources_set_contains_session_memory_compact_ctx_agent() -> None:
    assert "session_memory" in RECURSION_GUARDED_SOURCES
    assert "compact" in RECURSION_GUARDED_SOURCES
    assert "ctx_agent" in RECURSION_GUARDED_SOURCES
    assert "main" not in RECURSION_GUARDED_SOURCES
    assert "other" not in RECURSION_GUARDED_SOURCES
    assert "prompt_suggestion" not in RECURSION_GUARDED_SOURCES


def test_compact_request_dataclass_has_all_fields() -> None:
    names = {f.name for f in fields(CompactRequest)}
    assert names == {
        "model",
        "current_tokens",
        "target_tokens",
        "reason",
        "metadata",
    }
    # Default metadata must be an empty dict (field default_factory).
    req = CompactRequest(
        model="claude-opus-4-6",
        current_tokens=970_000,
        target_tokens=967_000,
        reason="warning",
    )
    assert req.metadata == {}


# ---------------------------------------------------------------------------
# extra coverage: sink exception path + target_tokens
# ---------------------------------------------------------------------------


def test_run_sink_exception_counted_as_failure() -> None:
    sink = FakeCompactSink(raise_exc=RuntimeError("network down"))
    ac = AutoCompactor(model="claude-opus-4-6", sink=sink)
    result = ac.run(current_tokens=970_000)
    assert result is not None
    assert result.success is False
    assert result.error is not None and "network down" in result.error
    assert ac.state.consecutive_failures == 1


def test_run_passes_warning_reason_first_then_error() -> None:
    captured: list[CompactRequest] = []

    class CaptureSink:
        def summarize(self, req: CompactRequest) -> CompactResult:
            captured.append(req)
            return _ok_result()

    ac = AutoCompactor(model="claude-opus-4-6", sink=CaptureSink())
    ac.run(current_tokens=970_000)  # warning zone
    ac.run(current_tokens=999_000)  # error zone (>= 987_000)
    assert [r.reason for r in captured] == ["warning", "error"]
    # target = 1_000_000 - 13_000 - 20_000 = 967_000
    assert all(r.target_tokens == 967_000 for r in captured)


def test_autocompact_state_defaults() -> None:
    s = AutoCompactState()
    assert s.consecutive_failures == 0
    assert s.total_compactions == 0
    assert s.total_reclaimed_tokens == 0
    assert s.last_trigger_ts == 0.0
    assert s.last_error == ""
    assert s.circuit_open is False


def test_save_noop_without_cache_dir() -> None:
    ac = AutoCompactor(model="claude-opus-4-6")
    # Should not raise
    ac.save()
    ac.load()
    assert ac.state.total_compactions == 0
