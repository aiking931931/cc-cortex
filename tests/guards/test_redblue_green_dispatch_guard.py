"""Edge-case tests for ``concinno.guards.redblue_green_dispatch_guard``.

The wave-2 ship cycle landed this guard with no dedicated test file
(the existing ``tests/cognitive/test_review_router.py`` covers a
different module). This file pins the load-bearing branches a future
refactor must not silently break:

- Feature kill-switch short-circuits with no spawn.
- ``Radius.SIMPLE`` short-circuits with no spawn.
- ``_parse_team_response`` tolerates malformed JSON.
- ``_parse_team_response`` skips entries with unknown axis enums.
- ``_parse_green_response`` falls back to ``HOLD`` on parse failure.
- ``_decide_verdict`` 5-state matrix:
  * pure framing-rejected findings → REJECT
  * fatal_count >= threshold → REJECT
  * 1 fatal below threshold → ACCEPT_DOWNGRADE
  * clean (no findings) → ACCEPT
  * 1 HIGH alone → HOLD

We never call a real Anthropic API — the dispatcher is a stub. This
keeps the tests offline and fast (matches the module's own design
contract: ``AgentDispatcher`` is a Protocol the caller injects).
"""

from __future__ import annotations

from unittest.mock import patch

from concinno.guards.redblue_green_dispatch_guard import (
    Axis,
    AxisFinding,
    FramingError,
    Radius,
    RedBlueGreenDispatchGuard,
    Verdict,
    _decide_verdict,
    _parse_green_response,
    _parse_team_response,
)

# ── feature kill switch ──────────────────────────────────────────


def test_feature_disabled_short_circuits_to_accept() -> None:
    """``feature_config.redblue_green_review.enabled=False`` → ACCEPT, 0 spawn."""

    def _never_called(*_a: object, **_kw: object) -> str:
        raise AssertionError("dispatcher must NOT be called when feature disabled")

    class _Stub:
        def dispatch(self, prompt: str, *, model: str = "opus", role: str) -> str:
            return _never_called(prompt, model=model, role=role)

    guard = RedBlueGreenDispatchGuard()
    with patch(
        "concinno.guards.redblue_green_dispatch_guard._feature_enabled",
        return_value=False,
    ):
        verdict = guard.review(
            decision_context="anything",
            radius=Radius.HIGH,
            dispatcher=_Stub(),
        )

    assert verdict.verdict == Verdict.ACCEPT
    assert verdict.spawn_count == 0
    assert "disabled" in verdict.rationale.lower()


# ── Simple radius short-circuit ──────────────────────────────────


def test_simple_radius_short_circuits_with_no_dispatch() -> None:
    """``Radius.SIMPLE`` skips review per ``rules/L1/redteam.md``."""

    class _Stub:
        def dispatch(self, prompt: str, *, model: str = "opus", role: str) -> str:
            raise AssertionError("dispatcher must NOT be called at SIMPLE radius")

    guard = RedBlueGreenDispatchGuard()
    verdict = guard.review(
        decision_context="trivial typo fix",
        radius=Radius.SIMPLE,
        dispatcher=_Stub(),
    )
    assert verdict.verdict == Verdict.ACCEPT
    assert verdict.spawn_count == 0
    assert verdict.findings_accepted == []
    assert verdict.findings_rejected_framing == []


# ── _parse_team_response malformed input ─────────────────────────


def test_parse_team_response_malformed_json_keeps_raw() -> None:
    """Malformed JSON produces empty findings but preserves raw_response."""
    raw = "this is not json {{{"
    report = _parse_team_response("red", raw)
    assert report.role == "red"
    assert report.findings == []
    assert report.summary == "(unparsed)"
    assert report.raw_response == raw


def test_parse_team_response_skips_unknown_axis() -> None:
    """An entry with an axis enum value not in ``Axis`` is dropped silently.

    Defensive: a model hallucinating a new axis name must not crash the
    aggregator. Other valid entries in the same response are still kept.
    """
    raw = (
        '{"summary": "ok", "findings": ['
        '{"axis": "nonsense_axis", "severity": "HIGH", "evidence": "x"},'
        '{"axis": "wired", "severity": "FATAL", "evidence": "y"}'
        "]}"
    )
    report = _parse_team_response("red", raw)
    assert len(report.findings) == 1
    assert report.findings[0].axis == Axis.WIRED
    assert report.findings[0].severity == "FATAL"


# ── _parse_green_response malformed input ────────────────────────


def test_parse_green_response_malformed_falls_back_to_hold() -> None:
    """A garbage green response must default to HOLD, not crash the verdict."""
    verdict, rationale, flags = _parse_green_response("not-json {")
    assert verdict == Verdict.HOLD
    assert "unparsed" in rationale.lower()
    assert flags == []


# ── _decide_verdict 5-state matrix ───────────────────────────────


def test_decide_verdict_pure_framing_rejected_returns_reject() -> None:
    """All findings flagged as framing errors → REJECT."""
    rejected = [
        (
            AxisFinding(
                axis=Axis.AI_CAPABILITY,
                severity="FATAL",
                evidence="cost framing wrong",
                framing_flag=FramingError.SCENARIO_PREMISE,
            ),
            FramingError.SCENARIO_PREMISE,
        ),
    ]
    verdict, rationale = _decide_verdict(
        accepted=[],
        rejected=rejected,
        fatal_threshold=3,
        green_verdict=None,
        green_pm_trust=0.70,
    )
    assert verdict == Verdict.REJECT
    assert "framing" in rationale.lower()


def test_decide_verdict_fatal_above_threshold_returns_reject() -> None:
    """``fatal_count >= fatal_threshold`` → REJECT regardless of HIGH count."""
    accepted = [
        AxisFinding(axis=Axis.REAL_DONE, severity="FATAL", evidence="a"),
        AxisFinding(axis=Axis.WIRED, severity="FATAL", evidence="b"),
        AxisFinding(axis=Axis.FUNCTIONAL, severity="FATAL", evidence="c"),
    ]
    verdict, rationale = _decide_verdict(
        accepted=accepted,
        rejected=[],
        fatal_threshold=3,
        green_verdict=None,
        green_pm_trust=0.70,
    )
    assert verdict == Verdict.REJECT
    assert "3" in rationale and "threshold" in rationale.lower()


def test_decide_verdict_one_fatal_below_threshold_returns_downgrade() -> None:
    """``1 <= fatal_count < threshold`` → ACCEPT_DOWNGRADE."""
    accepted = [
        AxisFinding(axis=Axis.UX_FRICTION, severity="FATAL", evidence="x"),
    ]
    verdict, _rat = _decide_verdict(
        accepted=accepted,
        rejected=[],
        fatal_threshold=3,
        green_verdict=None,
        green_pm_trust=0.70,
    )
    assert verdict == Verdict.ACCEPT_DOWNGRADE


def test_decide_verdict_clean_returns_accept() -> None:
    """No findings at all → ACCEPT."""
    verdict, rationale = _decide_verdict(
        accepted=[],
        rejected=[],
        fatal_threshold=3,
        green_verdict=None,
        green_pm_trust=0.70,
    )
    assert verdict == Verdict.ACCEPT
    assert "clean" in rationale.lower() or "no findings" in rationale.lower()


def test_decide_verdict_single_high_returns_hold() -> None:
    """Single HIGH-severity finding without a FATAL → HOLD pending verification."""
    accepted = [
        AxisFinding(axis=Axis.WIRED, severity="HIGH", evidence="orphan module"),
    ]
    verdict, _rat = _decide_verdict(
        accepted=accepted,
        rejected=[],
        fatal_threshold=3,
        green_verdict=None,
        green_pm_trust=0.70,
    )
    assert verdict == Verdict.HOLD
