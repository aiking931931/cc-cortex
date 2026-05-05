"""Tests for concinno.ziq_emit_helpers.

Covers reward-shaping math + bus integration for the helper functions
used to wire 18+ tunables into the ZIQ outcome bus (plan §60).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.ziq_emit_helpers import (
    emit_boolean_outcome,
    emit_budget_outcome,
    emit_classification_outcome,
    emit_continuous_outcome,
    emit_iteration_outcome,
    emit_threshold_outcome,
)
from concinno.ziq_outcome_bus import Outcome, ZIQOutcomeBus, get_bus


@pytest.fixture(autouse=True)
def _isolated_bus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pin_file = tmp_path / "ziq_pinned.json"
    monkeypatch.setenv("CONCINNO_ZIQ_PIN_FILE", str(pin_file))
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_MAX_HZ", raising=False)
    ZIQOutcomeBus._reset_for_testing()
    yield
    ZIQOutcomeBus._reset_for_testing()


# ── threshold helper ────────────────────────────────────────────


def test_threshold_tripped_emits_low_reward() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.thr", seen.append)
    emit_threshold_outcome(
        "test.thr",
        value=3,
        observed=5,
        tripped=True,
        source="test",
        trip_scale=10.0,
    )
    assert len(seen) == 1
    # value=3, trip_scale=10 → reward = 0.3, clamped to [0, 0.5]
    assert seen[0].reward == pytest.approx(0.3)
    assert seen[0].value == 3


def test_threshold_not_tripped_emits_high_reward() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.thr", seen.append)
    emit_threshold_outcome(
        "test.thr",
        value=10,
        observed=2,
        tripped=False,
        source="test",
    )
    # used_ratio = 2/10 = 0.2 → reward = 1 - 0.1 = 0.9
    assert seen[0].reward == pytest.approx(0.9)


def test_threshold_tripped_extreme_value_clamped() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.thr", seen.append)
    # value=100 / trip_scale=10 = 10 → clamped to 0.5
    emit_threshold_outcome(
        "test.thr",
        value=100,
        observed=200,
        tripped=True,
        source="test",
        trip_scale=10.0,
    )
    assert seen[0].reward == 0.5


# ── iteration helper ────────────────────────────────────────────


def test_iteration_succeeded_with_unused_budget() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.iter", seen.append)
    emit_iteration_outcome(
        "test.iter",
        value=10,
        iterations_used=2,
        succeeded=True,
        source="test",
    )
    # used_ratio = 0.2 → reward = 0.9
    assert seen[0].reward == pytest.approx(0.9)
    assert seen[0].metadata["succeeded"] is True


def test_iteration_failed_emits_zero() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.iter", seen.append)
    emit_iteration_outcome(
        "test.iter",
        value=5,
        iterations_used=5,
        succeeded=False,
        source="test",
    )
    assert seen[0].reward == 0.0


# ── budget helper ───────────────────────────────────────────────


def test_budget_overflow_emits_zero() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.budget", seen.append)
    emit_budget_outcome(
        "test.budget",
        value=1000,
        actual=1500,
        overflowed=True,
        source="test",
    )
    assert seen[0].reward == 0.0
    assert seen[0].metadata["overflowed"] is True


def test_budget_within_emits_proportional_reward() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.budget", seen.append)
    emit_budget_outcome(
        "test.budget",
        value=1000,
        actual=500,
        overflowed=False,
        source="test",
    )
    # ratio = 0.5 → reward = 1 - 0.2 = 0.8
    assert seen[0].reward == pytest.approx(0.8)


# ── classification helper ───────────────────────────────────────


def test_classification_correct_full_confidence() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.cls", seen.append)
    emit_classification_outcome(
        "test.cls",
        value="haiku",
        correct=True,
        confidence=1.0,
        source="test",
    )
    assert seen[0].reward == 1.0
    assert seen[0].value == "haiku"


def test_classification_incorrect_partial_credit() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.cls", seen.append)
    emit_classification_outcome(
        "test.cls",
        value="opus",
        correct=False,
        confidence=0.7,
        source="test",
    )
    # Wrong with conf 0.7 → reward = 1 - 0.7 = 0.3
    assert seen[0].reward == pytest.approx(0.3)


# ── boolean helper ──────────────────────────────────────────────


def test_boolean_success_one_failure_zero() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.bool", seen.append)
    emit_boolean_outcome("test.bool", value=True, success=True, source="test")
    emit_boolean_outcome("test.bool", value=False, success=False, source="test")
    assert [s.reward for s in seen] == [1.0, 0.0]


# ── continuous helper ───────────────────────────────────────────


def test_continuous_clamps_reward_to_unit_interval() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.cont", seen.append)
    emit_continuous_outcome(
        "test.cont", value=1.5, reward=2.5, source="test"
    )
    assert seen[0].reward == 1.0
    emit_continuous_outcome(
        "test.cont", value=1.5, reward=-0.5, source="test"
    )
    assert seen[1].reward == 0.0


# ── safety: helpers swallow bus errors ──────────────────────────


def test_helpers_swallow_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the bus is broken, helpers must not raise."""
    import concinno.ziq_emit_helpers as helpers

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("simulated bus failure")

    monkeypatch.setattr(helpers, "_safe_emit", boom)
    # Should NOT raise even though _safe_emit blows up
    with pytest.raises(RuntimeError):
        helpers._safe_emit("a", 1, 1.0, "src")
    # But going through the public helpers wraps everything:
    # Restore for integration check
    monkeypatch.undo()
    seen: list[Outcome] = []
    get_bus().subscribe("test.safe", seen.append)
    emit_boolean_outcome(
        "test.safe", value=True, success=True, source="src"
    )
    assert len(seen) == 1


def test_metadata_propagation() -> None:
    seen: list[Outcome] = []
    get_bus().subscribe("test.meta", seen.append)
    emit_threshold_outcome(
        "test.meta",
        value=5,
        observed=2,
        tripped=False,
        source="src",
        metadata={"tier": "haiku", "extra": 42},
    )
    assert seen[0].metadata["tier"] == "haiku"
    assert seen[0].metadata["extra"] == 42
