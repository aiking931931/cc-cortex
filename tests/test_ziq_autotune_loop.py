"""Tests for concinno.ziq_autotune_loop — outcome-driven cascade updater."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from concinno.ziq_autotune_loop import (
    AuditEntry,
    ZIQAutoTuneLoop,
    significant_delta,
)


@pytest.fixture(autouse=True)
def _isolate_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    import concinno.core.config as _ccfg_mod
    from concinno.core.config import get_config

    hooks_tmp = tmp_path / "hooks"
    hooks_tmp.mkdir(parents=True, exist_ok=True)
    _ccfg_mod._instance = None  # type: ignore[attr-defined]
    get_config(hooks_dir=str(hooks_tmp))
    # Also clear the ziq tuner cache so tests don't bleed state.
    from concinno.ziq_autotune_registry import clear_cache

    clear_cache()
    yield tmp_path
    _ccfg_mod._instance = None  # type: ignore[attr-defined]
    clear_cache()


class TestSignificantDelta:
    def test_same_value_not_significant(self) -> None:
        assert significant_delta(5, 5) is False
        assert significant_delta(True, True) is False
        assert significant_delta("a", "a") is False

    def test_small_numeric_below_threshold(self) -> None:
        # 1% change, below default 5% threshold.
        assert significant_delta(101, 100) is False

    def test_large_numeric_above_threshold(self) -> None:
        assert significant_delta(110, 100) is True

    def test_type_change_always_significant(self) -> None:
        # Cross-type values that are NOT equal → always significant.
        assert significant_delta("2", 2) is True
        assert significant_delta(None, 0) is True

    def test_bool_flip_significant(self) -> None:
        assert significant_delta(True, False) is True
        assert significant_delta(False, True) is True

    def test_string_change_significant(self) -> None:
        assert significant_delta("b", "a") is True

    def test_zero_old_threshold(self) -> None:
        # |1 - 0| / max(0, 1) = 1.0 — significant.
        assert significant_delta(1, 0) is True


class TestZIQAutoTuneLoopTick:
    def test_tick_empty_when_no_autotune_signal(self) -> None:
        loop = ZIQAutoTuneLoop()
        # Without CONCINNO_ZIQ_AUTOTUNE env var, every tuner is in
        # "preset" regime and is skipped.
        changes = loop.tick({})
        assert changes == []

    def test_tick_respects_cosmetic_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even with autotune enabled, a cosmetic-flagged feature must
        # never appear in tick() changes.
        monkeypatch.setenv("CONCINNO_ZIQ_AUTOTUNE", "1")
        from concinno.feature_config import FEATURE_META

        # session_summary is cosmetic.
        assert FEATURE_META["session_summary"].get("cosmetic") is True
        loop = ZIQAutoTuneLoop()
        changes = loop.tick({})
        for entry in changes:
            assert not entry.key.startswith("session_summary.")

    def test_tick_respects_ziq_autotunable_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONCINNO_ZIQ_AUTOTUNE", "1")
        from concinno.feature_config import FEATURE_META

        # handoff_required_guard is a safety feature (non-tunable).
        assert (
            FEATURE_META["handoff_required_guard"].get("ziq_autotunable")
            is False
        )
        loop = ZIQAutoTuneLoop()
        changes = loop.tick({})
        for entry in changes:
            assert not entry.key.startswith(
                "handoff_required_guard."
            )

    def test_tick_writes_audit_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: simulate enough observations to flip a real
        tunable target into the ``full`` regime and verify a JSONL line
        lands."""
        monkeypatch.setenv("CONCINNO_ZIQ_AUTOTUNE", "1")
        from concinno.ziq_autotune_registry import get_tuner

        # Feed 600 observations with a consistent positive reward on a
        # shifted value so the continuous tuner learns to move.
        tuner = get_tuner(
            "delivery.gate.max_iterations",
            store_dir=tmp_path / "ziq_tuners",
        )
        for _ in range(600):
            tuner.record(10.0, outcome=0.9)
        # sanity: regime should now be "full"
        assert tuner.current_regime() == "full"
        # The suggestion should differ from preset (5) by a lot.
        assert abs(tuner.suggest() - 5.0) >= 0.5

        log_path = tmp_path / "autotune.jsonl"
        loop = ZIQAutoTuneLoop(log_path=log_path)
        changes = loop.tick({})
        # At least our target flipped. Other autotunable targets with
        # no observations stay in preset regime.
        dotted = "delivery_gate.max_iterations"
        found = [e for e in changes if e.key == dotted]
        assert len(found) == 1, (
            f"Expected {dotted} in changes, got: "
            f"{[c.key for c in changes]}"
        )
        assert log_path.is_file()
        log_lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(log_lines) >= 1
        entry = json.loads(log_lines[0])
        assert entry["key"] == dotted
        assert entry["origin"][0] == "ziq"


class TestAuditEntry:
    def test_as_json_line_parses(self) -> None:
        entry = AuditEntry(
            ts="2026-04-23T00:00:00+00:00",
            key="token_gate.agent_threshold",
            old=140000,
            new=150000,
            reason="test",
            alpha_t=0.5,
            origin=("ziq", "autotune", "full"),
        )
        line = entry.as_json_line()
        data = json.loads(line)
        assert data["key"] == "token_gate.agent_threshold"
        assert data["origin"] == ["ziq", "autotune", "full"]

    def test_handles_complex_values(self) -> None:
        entry = AuditEntry(
            ts="now",
            key="k",
            old={1, 2, 3},  # set needs fallback encoder
            new=[1, 2],
            reason="",
            alpha_t=0.0,
            origin=(),
        )
        line = entry.as_json_line()
        data: dict[str, Any] = json.loads(line)
        assert set(data["old"]) == {1, 2, 3}
