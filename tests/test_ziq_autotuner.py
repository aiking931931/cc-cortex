"""Tests for concinno.ziq_autotuner — three-regime behavior + persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno.ziq_autotuner import (
    AutoTuneObservation,
    ZIQAutoTuner,
    is_autotune_enabled,
)


@pytest.fixture
def tuner_env(tmp_path, monkeypatch):
    """Enable auto-tune with isolated storage directory per test."""
    monkeypatch.setenv("CONCINNO_ZIQ_AUTOTUNE", "1")
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture
def disabled_env(tmp_path, monkeypatch):
    """Explicitly disable auto-tune so ``current_regime`` collapses to preset."""
    monkeypatch.delenv("CONCINNO_ZIQ_AUTOTUNE", raising=False)
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    yield tmp_path


# ── is_autotune_enabled ─────────────────────────────────────────────


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("bogus", False),
])
def test_is_autotune_enabled_parses_env(monkeypatch, val, expected):
    monkeypatch.setenv("CONCINNO_ZIQ_AUTOTUNE", val)
    assert is_autotune_enabled() is expected


def test_is_autotune_enabled_absent_env(monkeypatch):
    monkeypatch.delenv("CONCINNO_ZIQ_AUTOTUNE", raising=False)
    assert is_autotune_enabled() is False


# ── Three-regime continuous ─────────────────────────────────────────


def test_continuous_preset_regime(tuner_env):
    t = ZIQAutoTuner(
        "target_cont", preset=10.0, vmin=1.0, vmax=100.0,
        tunable_threshold=5, full_threshold=10, auto_persist=False,
    )
    assert t.current_regime() == "preset"
    assert t.suggest() == 10.0
    for _ in range(4):
        t.record(20.0, 1.0)
    assert t.current_regime() == "preset"
    assert t.suggest() == 10.0


def test_continuous_conservative_shrinks_toward_preset(tuner_env):
    t = ZIQAutoTuner(
        "t_conserv", preset=10.0, vmin=1.0, vmax=100.0,
        tunable_threshold=5, full_threshold=20, auto_persist=False,
    )
    for _ in range(7):
        t.record(50.0, 1.0)  # rewards say 50 is great
    assert t.current_regime() == "conservative"
    s = t.suggest()
    # Conservative = 0.7*preset + 0.3*estimate — strictly between them.
    assert 10.0 < s < 50.0


def test_continuous_full_trusts_estimate(tuner_env):
    t = ZIQAutoTuner(
        "t_full", preset=10.0, vmin=1.0, vmax=100.0,
        tunable_threshold=5, full_threshold=10, auto_persist=False,
    )
    for _ in range(15):
        t.record(50.0, 1.0)
    assert t.current_regime() == "full"
    assert t.suggest() > 40.0  # fully converged toward 50


def test_continuous_clamps_to_vmin_vmax(tuner_env):
    t = ZIQAutoTuner(
        "t_clamp", preset=50.0, vmin=10.0, vmax=60.0,
        tunable_threshold=2, full_threshold=4, auto_persist=False,
    )
    for _ in range(10):
        t.record(500.0, 1.0)  # crazy high value
    assert t.suggest() <= 60.0
    assert t.suggest() >= 10.0


# ── Three-regime discrete ───────────────────────────────────────────


def test_discrete_preset_regime(tuner_env):
    t = ZIQAutoTuner(
        "t_disc", preset="A", choices=["A", "B", "C"],
        tunable_threshold=5, full_threshold=10, auto_persist=False,
    )
    assert t.suggest() == "A"
    for _ in range(3):
        t.record("B", 1.0)
    assert t.suggest() == "A"  # still preset (n<5)


def test_discrete_conservative_requires_2x_evidence(tuner_env):
    t = ZIQAutoTuner(
        "t_disc_cons", preset="A", choices=["A", "B", "C"],
        tunable_threshold=5, full_threshold=20, auto_persist=False,
    )
    # Equal samples for A and B — conservative should stay on preset.
    for _ in range(3):
        t.record("A", 0.3)
    for _ in range(3):
        t.record("B", 0.9)
    assert t.current_regime() == "conservative"
    assert t.suggest() == "A"  # B wins reward but lacks 2x count

    # Push B past the 2x threshold.
    for _ in range(4):
        t.record("B", 0.9)
    # Now B count=7, A count=3 → 7 >= 2*3.
    assert t.suggest() == "B"


def test_discrete_full_picks_greedy_winner(tuner_env):
    t = ZIQAutoTuner(
        "t_disc_full", preset="A", choices=["A", "B", "C"],
        tunable_threshold=5, full_threshold=10, auto_persist=False,
    )
    for _ in range(5):
        t.record("A", 0.1)
    for _ in range(8):
        t.record("C", 0.95)
    assert t.current_regime() == "full"
    assert t.suggest() == "C"


# ── Boolean target ──────────────────────────────────────────────────


def test_boolean_kind_inference(tuner_env):
    t = ZIQAutoTuner(
        "t_bool", preset=False,
        tunable_threshold=3, full_threshold=6, auto_persist=False,
    )
    assert t.kind == "boolean"
    assert t.suggest() is False
    for _ in range(4):
        t.record(True, 1.0)  # True outcome hot
    # Need 2x evidence to flip away from False preset.
    assert t.suggest() is False
    for _ in range(4):
        t.record(True, 1.0)
    assert t.suggest() is True


# ── Disabled env ────────────────────────────────────────────────────


def test_disabled_env_collapses_to_preset(disabled_env):
    t = ZIQAutoTuner(
        "t_disabled", preset=7.0, vmin=1.0, vmax=20.0,
        tunable_threshold=2, full_threshold=4, auto_persist=False,
    )
    for _ in range(20):
        t.record(15.0, 1.0)
    assert t.current_regime() == "preset"
    assert t.suggest() == 7.0


# ── Persistence: append-only JSONL + reload ─────────────────────────


def test_persistence_append_and_reload(tuner_env):
    t = ZIQAutoTuner(
        "t_persist", preset=10.0, vmin=1.0, vmax=100.0,
        tunable_threshold=2, full_threshold=4, auto_persist=True,
    )
    for _ in range(8):
        t.record(30.0, 0.9)
    path = tuner_env / "t_persist.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 8
    # Each line parses as a record.
    for line in lines:
        rec = json.loads(line)
        assert rec["value"] == 30.0
        assert rec["outcome"] == 0.9

    # Reload by creating a fresh instance on same path.
    t2 = ZIQAutoTuner(
        "t_persist", preset=10.0, vmin=1.0, vmax=100.0,
        tunable_threshold=2, full_threshold=4, auto_persist=False,
    )
    assert t2.n == 8
    # Should be in full regime now and suggesting near 30.
    assert t2.current_regime() == "full"
    assert 20.0 < t2.suggest() <= 30.0


def test_persistence_survives_malformed_line(tuner_env):
    path = tuner_env / "corrupt.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"value": 5.0, "outcome": 0.5}) + "\n"
        + "not json\n"
        + json.dumps({"value": 7.0, "outcome": 1.0}) + "\n",
        encoding="utf-8",
    )
    t = ZIQAutoTuner(
        "corrupt", preset=1.0, vmin=0.0, vmax=100.0,
        tunable_threshold=1, full_threshold=2, auto_persist=False,
    )
    # Malformed line skipped; clean lines loaded.
    assert t.n == 2


# ── Snapshot diagnostics ────────────────────────────────────────────


def test_snapshot_shape(tuner_env):
    t = ZIQAutoTuner(
        "t_snap", preset="A", choices=["A", "B"],
        tunable_threshold=3, full_threshold=6, auto_persist=False,
    )
    for _ in range(4):
        t.record("A", 0.5)
    snap = t.snapshot()
    assert snap["target"] == "t_snap"
    assert snap["kind"] == "discrete"
    assert snap["n"] == 4
    assert "regime" in snap
    assert "arm_rewards" in snap
    assert "arm_counts" in snap


# ── Validation / error handling ────────────────────────────────────


def test_threshold_ordering_validated():
    with pytest.raises(ValueError):
        ZIQAutoTuner("bad", preset=1.0, tunable_threshold=10, full_threshold=5)


def test_discrete_requires_choices():
    with pytest.raises(ValueError):
        ZIQAutoTuner("no_choices", preset="X", kind="discrete")


def test_continuous_vmin_vmax_ordering():
    with pytest.raises(ValueError):
        ZIQAutoTuner("bad_bounds", preset=1.0, vmin=10.0, vmax=1.0)


def test_outcome_clamped_to_unit_interval(tuner_env):
    t = ZIQAutoTuner(
        "t_clamp_out", preset="A", choices=["A", "B"],
        tunable_threshold=2, full_threshold=4, auto_persist=False,
    )
    t.record("B", 99.0)  # out of range
    t.record("B", -99.0)
    snap = t.snapshot()
    # Both clamped to [0, 1]; arm_rewards avg should be within [0, 1].
    for r in snap["arm_rewards"].values():
        assert 0.0 <= r <= 1.0


def test_empty_history_returns_preset(tuner_env):
    t = ZIQAutoTuner(
        "t_empty", preset=42.0, vmin=0.0, vmax=100.0,
        tunable_threshold=0, full_threshold=1, auto_persist=False,
    )
    # Even with tunable_threshold=0, empty history returns preset.
    assert t.suggest() == 42.0


# ── AutoTuneObservation is frozen ──────────────────────────────────


def test_observation_is_frozen():
    obs = AutoTuneObservation(value=5, outcome=0.8, context={})
    with pytest.raises(Exception):
        obs.value = 10  # type: ignore[misc]


# ── Disk persistence survives OSError gracefully ───────────────────


def test_record_survives_disk_failure(monkeypatch, tuner_env):
    t = ZIQAutoTuner(
        "t_disk_fail", preset=10.0, vmin=0.0, vmax=100.0,
        tunable_threshold=1, full_threshold=2, auto_persist=True,
    )

    def _raise_oserror(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", _raise_oserror)
    # Should not raise, just log to in-memory state.
    t.record(20.0, 0.5)
    assert t.n == 1


def test_store_dir_override_param_overrides_env(monkeypatch, tmp_path):
    other = tmp_path / "other"
    env_dir = tmp_path / "from_env"
    monkeypatch.setenv("CONCINNO_ZIQ_AUTOTUNE", "1")
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(env_dir))
    t = ZIQAutoTuner(
        "t_override", preset=1.0, vmin=0.0, vmax=10.0,
        tunable_threshold=1, full_threshold=2,
        store_dir=other,
        auto_persist=True,
    )
    t.record(5.0, 1.0)
    assert (other / "t_override.jsonl").exists()
    assert not (env_dir / "t_override.jsonl").exists()


# ── FTRL outcome emit (MEMORY 2h P3-a root-cause fix) ──────────────


def test_record_emits_ftrl_outcome_to_ziq_state(
    monkeypatch, tmp_path,
):
    """``record()`` must append one FTRL event per call to ``ziq_state/``.

    MEMORY 2h P3-a root-cause fix: prior to this wiring the autotuner
    persisted observations to ``ziq_tuners/<target>.jsonl`` only, which
    the downstream FTRL loader (``concinno.ziq.persist.load_ftrl_state``)
    never reads. The bridge here closes that gap so every ``record()``
    call leaves an event on the shared FTRL trail keyed on the tuner's
    ``target``.

    Verified end-to-end:
        * file lands at the env-pinned ``CONCINNO_ZIQ_STATE_DIR`` path,
        * schema matches the 2026-05-07 baseline event exactly
          (feature / key / weight_before / weight_after / signal / ts
          / posterior_components),
        * signal is centred to ``[-1, 1]`` so outcome=1.0 → signal=+1.0
          (matches the existing ``agent_invariants_ftrl.jsonl`` event).
    """
    import json as _json

    from concinno.ziq import persist as _persist

    state_dir_override = tmp_path / "ziq_state_emit"
    monkeypatch.setenv("CONCINNO_ZIQ_AUTOTUNE", "1")
    monkeypatch.setenv("CONCINNO_ZIQ_STATE_DIR", str(state_dir_override))
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path / "ziq_tuners"))
    monkeypatch.delenv("CONCINNO_ZIQ_PERSIST_DISABLED", raising=False)

    t = ZIQAutoTuner(
        "t_emit",
        preset="A",
        choices=["A", "B"],
        tunable_threshold=1,
        full_threshold=2,
        auto_persist=True,
    )
    t.record("B", 1.0, context={"agent_id": "test-emit", "reason": "smoke"})

    jsonl = _persist.jsonl_path("t_emit")
    assert jsonl.exists(), f"FTRL emit target {jsonl} not created"
    rows = jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    rec = _json.loads(rows[0])

    # Schema parity with 2026-05-07 baseline event.
    assert rec["feature"] == "t_emit"
    assert rec["key"] == "B"
    assert rec["signal"] == pytest.approx(1.0, abs=1e-9)
    assert "ts" in rec
    assert "weight_before" in rec
    assert "weight_after" in rec
    # weight_before is the arm's weight before its first update; the
    # FTRL-Proximal arm starts at z=0, n=0 so weight()==0.0.
    assert rec["weight_before"] == pytest.approx(0.0, abs=1e-9)
    # Context propagates into posterior_components so the FTRL audit
    # trail can trace the originating call.
    assert rec["posterior_components"]["agent_id"] == "test-emit"


def test_record_emit_failure_does_not_break_tuner(
    monkeypatch, tuner_env,
):
    """If the persist layer raises, ``record()`` still succeeds.

    Telemetry is best-effort by contract — a broken FTRL emit must not
    drop the in-memory observation or propagate the error.
    """
    from concinno.ziq import persist as _persist

    def _boom(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("simulated persist crash")

    monkeypatch.setattr(_persist, "record_ftrl_update", _boom)

    t = ZIQAutoTuner(
        "t_emit_fail",
        preset=10.0, vmin=0.0, vmax=100.0,
        tunable_threshold=1, full_threshold=2,
        auto_persist=True,
    )
    # Must not raise; in-memory observation must still land.
    t.record(20.0, 0.75)
    assert t.n == 1
