"""Tests for the standalone autocompact circuit breaker.

Covers :class:`AutoCompactCircuitBreaker` in isolation plus its wiring
into :class:`AutoCompactor`. Ported semantics from
``autoCompact.ts:67-70``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_cortex.cache.autocompact import (
    AUTOCOMPACT_BUFFER_TOKENS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    AutoCompactCircuitBreaker,
    AutoCompactExhausted,
    AutoCompactor,
    CompactRequest,
    CompactResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FailingSink:
    """Sink that always returns a failed result."""

    def __init__(self, error: str = "prompt_too_long") -> None:
        self.error = error
        self.calls = 0

    def summarize(self, req: CompactRequest) -> CompactResult:
        self.calls += 1
        return CompactResult(
            success=False, summary_tokens=0, reclaimed_tokens=0, error=self.error
        )


class _SuccessSink:
    """Sink that always returns success."""

    def __init__(self) -> None:
        self.calls = 0

    def summarize(self, req: CompactRequest) -> CompactResult:
        self.calls += 1
        return CompactResult(
            success=True, summary_tokens=500, reclaimed_tokens=50_000
        )


def _tokens_above_threshold(ac: AutoCompactor) -> int:
    return ac.model_budget - AUTOCOMPACT_BUFFER_TOKENS - 5_000


# ---------------------------------------------------------------------------
# Standalone breaker API
# ---------------------------------------------------------------------------


def test_breaker_defaults_match_cc_parity() -> None:
    b = AutoCompactCircuitBreaker()
    assert b.max_failures == MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES == 3
    assert b.failures == 0
    assert b.disabled is False
    assert b.last_failure_reason == ""
    assert b.disabled_at_session is None
    assert b.should_attempt() is True


def test_breaker_one_failure_still_attempts() -> None:
    b = AutoCompactCircuitBreaker()
    tripped = b.record_failure("boom", "sess-1")
    assert tripped is False
    assert b.should_attempt() is True
    assert b.failures == 1
    assert b.last_failure_reason == "boom"


def test_breaker_two_failures_still_attempts() -> None:
    b = AutoCompactCircuitBreaker()
    b.record_failure("boom1", "s")
    tripped = b.record_failure("boom2", "s")
    assert tripped is False
    assert b.should_attempt() is True
    assert b.failures == 2


def test_breaker_trips_on_third_failure() -> None:
    b = AutoCompactCircuitBreaker()
    b.record_failure("boom1", "sess-x")
    b.record_failure("boom2", "sess-x")
    tripped = b.record_failure("boom3", "sess-x")
    assert tripped is True
    assert b.disabled is True
    assert b.should_attempt() is False
    assert b.failures == 3
    assert b.disabled_at_session == "sess-x"
    assert b.last_failure_reason == "boom3"


def test_breaker_record_failure_after_trip_returns_false() -> None:
    b = AutoCompactCircuitBreaker()
    b.record_failure("a", "s")
    b.record_failure("b", "s")
    b.record_failure("c", "s")  # trip
    # Subsequent failures are noise, not new trips.
    again = b.record_failure("d", "s")
    assert again is False
    assert b.failures == 3  # counter does NOT overflow past max
    assert b.last_failure_reason == "d"  # but reason still updates


def test_breaker_success_resets_counter_but_not_disabled_when_tripped() -> None:
    b = AutoCompactCircuitBreaker()
    b.record_failure("x", "s")
    b.record_failure("y", "s")
    b.record_failure("z", "s")  # trip
    assert b.disabled is True
    b.record_success()
    # Counter cleared but the latch remains open.
    assert b.failures == 0
    assert b.disabled is True
    assert b.should_attempt() is False


def test_breaker_success_before_trip_clears_counter() -> None:
    b = AutoCompactCircuitBreaker()
    b.record_failure("x", "s")
    b.record_failure("y", "s")
    b.record_success()
    assert b.failures == 0
    # A fresh 3-failure sequence can trip.
    b.record_failure("a", "s")
    b.record_failure("b", "s")
    tripped = b.record_failure("c", "s")
    assert tripped is True


def test_breaker_reset_clears_everything() -> None:
    b = AutoCompactCircuitBreaker()
    for i in range(3):
        b.record_failure(f"err{i}", "sess-x")
    assert b.disabled is True
    b.reset()
    assert b.failures == 0
    assert b.disabled is False
    assert b.last_failure_reason == ""
    assert b.disabled_at_session is None
    assert b.should_attempt() is True


def test_breaker_custom_max_failures() -> None:
    b = AutoCompactCircuitBreaker(max_failures=2)
    b.record_failure("a", "s")
    tripped = b.record_failure("b", "s")
    assert tripped is True
    assert b.disabled is True


# ---------------------------------------------------------------------------
# AutoCompactor integration
# ---------------------------------------------------------------------------


def test_autocompactor_starts_with_healthy_breaker() -> None:
    ac = AutoCompactor()
    assert ac.breaker.should_attempt() is True
    assert ac.breaker.failures == 0


def test_autocompactor_failure_increments_breaker() -> None:
    sink = _FailingSink()
    ac = AutoCompactor(sink=sink, session_id="sess-int-1")
    tokens = _tokens_above_threshold(ac)
    # First failure — does not trip.
    result = ac.run(current_tokens=tokens)
    assert result is not None
    assert result.success is False
    assert ac.breaker.failures == 1
    assert ac.breaker.should_attempt() is True


def test_autocompactor_three_failures_trip_and_raise() -> None:
    sink = _FailingSink()
    ac = AutoCompactor(sink=sink, session_id="sess-int-2")
    tokens = _tokens_above_threshold(ac)
    ac.run(current_tokens=tokens)  # 1
    ac.run(current_tokens=tokens)  # 2
    with pytest.raises(AutoCompactExhausted):
        ac.run(current_tokens=tokens)  # 3 — trips
    assert ac.breaker.disabled is True
    assert ac.breaker.disabled_at_session == "sess-int-2"


def test_autocompactor_tripped_breaker_blocks_further_runs() -> None:
    sink = _FailingSink()
    ac = AutoCompactor(sink=sink, session_id="sess-int-3")
    tokens = _tokens_above_threshold(ac)
    ac.run(current_tokens=tokens)
    ac.run(current_tokens=tokens)
    with pytest.raises(AutoCompactExhausted):
        ac.run(current_tokens=tokens)
    # Breaker is now open. Further attempts must raise without calling sink.
    pre_calls = sink.calls
    with pytest.raises(AutoCompactExhausted):
        ac.run(current_tokens=tokens)
    assert sink.calls == pre_calls, "sink must not be called when breaker is open"


def test_autocompactor_success_resets_inline_counter_and_breaker() -> None:
    # Flip-flop sink: fail, fail, succeed.
    class Flip:
        def __init__(self) -> None:
            self.n = 0

        def summarize(self, req: CompactRequest) -> CompactResult:
            self.n += 1
            if self.n <= 2:
                return CompactResult(
                    success=False, summary_tokens=0, reclaimed_tokens=0, error="e"
                )
            return CompactResult(
                success=True, summary_tokens=500, reclaimed_tokens=50_000
            )

    ac = AutoCompactor(sink=Flip(), session_id="flip-sess")
    tokens = _tokens_above_threshold(ac)
    ac.run(current_tokens=tokens)
    ac.run(current_tokens=tokens)
    assert ac.breaker.failures == 2
    ac.run(current_tokens=tokens)  # success
    assert ac.breaker.failures == 0
    assert ac.breaker.should_attempt() is True


def test_autocompactor_reset_breaker_clears_both_flags(tmp_path: Path) -> None:
    sink = _FailingSink()
    ac = AutoCompactor(
        cache_dir=str(tmp_path),
        session_id="reset-sess",
        sink=sink,
    )
    tokens = _tokens_above_threshold(ac)
    ac.run(current_tokens=tokens)
    ac.run(current_tokens=tokens)
    with pytest.raises(AutoCompactExhausted):
        ac.run(current_tokens=tokens)
    assert ac.breaker.disabled is True
    assert ac.state.circuit_open is True

    ac.reset_breaker()
    assert ac.breaker.disabled is False
    assert ac.state.circuit_open is False
    assert ac.state.consecutive_failures == 0
    assert ac.breaker.failures == 0


# ---------------------------------------------------------------------------
# StateStore persistence
# ---------------------------------------------------------------------------


def test_breaker_state_persists_across_instance_recreation(tmp_path: Path) -> None:
    sid = "persist-sess-1"

    # First instance: fail twice.
    sink1 = _FailingSink()
    ac1 = AutoCompactor(
        cache_dir=str(tmp_path), session_id=sid, sink=sink1
    )
    tokens = _tokens_above_threshold(ac1)
    ac1.run(current_tokens=tokens)
    ac1.run(current_tokens=tokens)
    ac1.save()
    assert ac1.breaker.failures == 2

    # New instance: load and confirm the breaker state survived.
    sink2 = _FailingSink()
    ac2 = AutoCompactor(
        cache_dir=str(tmp_path), session_id=sid, sink=sink2
    )
    ac2.load()
    assert ac2.breaker.failures == 2
    assert ac2.breaker.should_attempt() is True
    # Third failure trips the reconstituted breaker.
    with pytest.raises(AutoCompactExhausted):
        ac2.run(current_tokens=tokens)
    assert ac2.breaker.disabled is True


def test_breaker_trip_persists_across_recreation(tmp_path: Path) -> None:
    sid = "persist-sess-2"
    sink1 = _FailingSink()
    ac1 = AutoCompactor(
        cache_dir=str(tmp_path), session_id=sid, sink=sink1
    )
    tokens = _tokens_above_threshold(ac1)
    ac1.run(current_tokens=tokens)
    ac1.run(current_tokens=tokens)
    with pytest.raises(AutoCompactExhausted):
        ac1.run(current_tokens=tokens)
    assert ac1.breaker.disabled is True
    assert ac1.breaker.disabled_at_session == sid

    ac2 = AutoCompactor(
        cache_dir=str(tmp_path), session_id=sid, sink=_FailingSink()
    )
    ac2.load()
    assert ac2.breaker.disabled is True
    assert ac2.breaker.disabled_at_session == sid
    with pytest.raises(AutoCompactExhausted):
        ac2.run(current_tokens=tokens)


def test_breaker_custom_max_failures_via_constructor() -> None:
    breaker = AutoCompactCircuitBreaker(max_failures=2)
    sink = _FailingSink()
    ac = AutoCompactor(
        sink=sink, session_id="custom-sess", breaker=breaker
    )
    tokens = _tokens_above_threshold(ac)
    ac.run(current_tokens=tokens)  # 1
    # The custom breaker trips at 2, but the legacy inline counter
    # trips at MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES (3). The breaker
    # should cause the trip on the 2nd failure anyway because
    # ``run()`` honors ``breaker.record_failure``.
    with pytest.raises(AutoCompactExhausted):
        ac.run(current_tokens=tokens)  # 2 — trips breaker
    assert ac.breaker.disabled is True


# ---------------------------------------------------------------------------
# Regression: existing behaviour not broken
# ---------------------------------------------------------------------------


def test_success_path_still_returns_result_with_breaker_wired() -> None:
    sink = _SuccessSink()
    ac = AutoCompactor(sink=sink, session_id="regression-sess")
    tokens = _tokens_above_threshold(ac)
    result = ac.run(current_tokens=tokens)
    assert result is not None and result.success is True
    assert ac.breaker.failures == 0
    assert ac.breaker.should_attempt() is True


def test_noop_path_does_not_touch_breaker() -> None:
    sink = _FailingSink()
    ac = AutoCompactor(sink=sink, session_id="noop-sess")
    # Token count well under threshold.
    result = ac.run(current_tokens=1_000)
    assert result is None
    assert ac.breaker.failures == 0
    assert sink.calls == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
