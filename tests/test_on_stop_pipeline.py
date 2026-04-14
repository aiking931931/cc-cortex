"""Tests for on_stop async pipeline — F4 circuit breaker + parallel execution."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from cc_cortex.hooks.on_stop import (
    _CircuitState,
    _fallback_sequential,
    _load_circuit_states,
    _run_module,
    _run_pipeline,
    _save_circuit_states,
    _StopModule,
    pipeline_report,
)

# ── CircuitState ────────────────────────────────────────────


class TestCircuitState:
    def test_initial_state_not_open(self):
        cs = _CircuitState()
        assert not cs.is_open()

    def test_below_threshold_not_open(self):
        cs = _CircuitState(consecutive_failures=2, last_failure_ts=time.time())
        assert not cs.is_open()

    def test_at_threshold_and_recent_is_open(self):
        cs = _CircuitState(consecutive_failures=3, last_failure_ts=time.time())
        assert cs.is_open()

    def test_at_threshold_but_old_is_closed(self):
        cs = _CircuitState(
            consecutive_failures=3,
            last_failure_ts=time.time() - 120,  # 2 min ago > 60s cooldown
        )
        assert not cs.is_open()

    def test_record_success_resets(self):
        cs = _CircuitState(consecutive_failures=5, last_failure_ts=time.time())
        cs.record_success()
        assert cs.consecutive_failures == 0
        assert not cs.is_open()

    def test_record_failure_increments(self):
        cs = _CircuitState()
        cs.record_failure()
        assert cs.consecutive_failures == 1
        assert cs.last_failure_ts > 0

    def test_record_failure_to_threshold(self):
        cs = _CircuitState()
        for _ in range(3):
            cs.record_failure()
        assert cs.is_open()


# ── Persistence ─────────────────────────────────────────────


class TestCircuitPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "circuit.json")
        states = {
            "mod_a": _CircuitState(consecutive_failures=2, last_failure_ts=100.0),
            "mod_b": _CircuitState(consecutive_failures=0),  # should not be saved
        }
        with patch("cc_cortex.hooks.on_stop._CIRCUIT_STATE_PATH", path):
            _save_circuit_states(states)
            loaded = _load_circuit_states()

        assert "mod_a" in loaded
        assert loaded["mod_a"].consecutive_failures == 2
        assert loaded["mod_a"].last_failure_ts == 100.0
        assert "mod_b" not in loaded  # zero failures not persisted

    def test_load_missing_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        with patch("cc_cortex.hooks.on_stop._CIRCUIT_STATE_PATH", path):
            loaded = _load_circuit_states()
        assert loaded == {}

    def test_load_corrupt_file(self, tmp_path):
        path = str(tmp_path / "corrupt.json")
        with open(path, "w") as f:
            f.write("not json{{{")
        with patch("cc_cortex.hooks.on_stop._CIRCUIT_STATE_PATH", path):
            loaded = _load_circuit_states()
        assert loaded == {}


# ── _run_module ─────────────────────────────────────────────


class TestRunModule:
    @pytest.mark.asyncio
    async def test_successful_module(self):
        mod = _StopModule("test", lambda: "ok", timeout_s=5.0)
        circuit = _CircuitState()
        await _run_module(mod, circuit)
        assert mod.result == "ok"
        assert not mod.error
        assert not mod.skipped
        assert not mod.timed_out
        assert mod.elapsed_ms >= 0  # fast lambda may be 0.0ms
        assert circuit.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_module_exception(self):
        def _boom():
            raise ValueError("boom")

        mod = _StopModule("test", _boom, timeout_s=5.0)
        circuit = _CircuitState()
        await _run_module(mod, circuit)
        assert mod.error == "boom"
        assert not mod.timed_out
        assert circuit.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_module_timeout(self):
        def _slow():
            time.sleep(2)

        mod = _StopModule("test", _slow, timeout_s=0.1)
        circuit = _CircuitState()
        await _run_module(mod, circuit)
        assert mod.timed_out
        assert "timeout" in mod.error
        assert circuit.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_circuit_open_skips(self):
        mod = _StopModule("test", lambda: "should not run", timeout_s=5.0)
        circuit = _CircuitState(consecutive_failures=3, last_failure_ts=time.time())
        await _run_module(mod, circuit)
        assert mod.skipped
        assert mod.result is None

    @pytest.mark.asyncio
    async def test_circuit_cooldown_expired_runs(self):
        mod = _StopModule("test", lambda: "ran", timeout_s=5.0)
        circuit = _CircuitState(
            consecutive_failures=3,
            last_failure_ts=time.time() - 120,  # expired
        )
        await _run_module(mod, circuit)
        assert not mod.skipped
        assert mod.result == "ran"
        assert circuit.consecutive_failures == 0  # success resets


# ── _run_pipeline ───────────────────────────────────────────


class TestRunPipeline:
    @pytest.mark.asyncio
    async def test_parallel_execution(self, tmp_path):
        """Verify modules actually run in parallel (total time < sum of individual)."""
        path = str(tmp_path / "circuit.json")

        def _sleep_100ms():
            time.sleep(0.1)
            return "done"

        modules = [
            _StopModule(f"mod_{i}", _sleep_100ms, timeout_s=5.0) for i in range(4)
        ]

        t0 = time.monotonic()
        with patch("cc_cortex.hooks.on_stop._CIRCUIT_STATE_PATH", path):
            result = await _run_pipeline(modules)
        elapsed = time.monotonic() - t0

        # 4 modules × 100ms each = 400ms sequential, should be ~100ms parallel
        assert elapsed < 0.35  # generous margin
        for mod in result:
            assert mod.result == "done"
            assert not mod.error

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self, tmp_path):
        path = str(tmp_path / "circuit.json")

        def _ok():
            return "ok"

        def _fail():
            raise RuntimeError("fail")

        modules = [
            _StopModule("good", _ok, timeout_s=5.0),
            _StopModule("bad", _fail, timeout_s=5.0),
        ]
        with patch("cc_cortex.hooks.on_stop._CIRCUIT_STATE_PATH", path):
            result = await _run_pipeline(modules)

        assert result[0].result == "ok"
        assert result[0].error == ""
        assert result[1].error == "fail"

    @pytest.mark.asyncio
    async def test_circuit_state_persisted(self, tmp_path):
        path = str(tmp_path / "circuit.json")

        def _fail():
            raise RuntimeError("fail")

        modules = [_StopModule("failing", _fail, timeout_s=5.0)]
        with patch("cc_cortex.hooks.on_stop._CIRCUIT_STATE_PATH", path):
            await _run_pipeline(modules)

        # Verify state was written
        with open(path, "r") as f:
            data = json.load(f)
        assert "failing" in data
        assert data["failing"]["consecutive_failures"] == 1

    @pytest.mark.asyncio
    async def test_empty_modules(self, tmp_path):
        path = str(tmp_path / "circuit.json")
        with patch("cc_cortex.hooks.on_stop._CIRCUIT_STATE_PATH", path):
            result = await _run_pipeline([])
        assert result == []


# ── pipeline_report ─────────────────────────────────────────


class TestPipelineReport:
    def test_success_report(self):
        mod = _StopModule("test", lambda: None)
        mod.elapsed_ms = 42.0
        report = pipeline_report([mod])
        assert "✅" in report
        assert "42ms" in report

    def test_timeout_report(self):
        mod = _StopModule("test", lambda: None, timeout_s=10.0)
        mod.timed_out = True
        mod.elapsed_ms = 10000.0
        mod.error = "timeout after 10.0s"
        report = pipeline_report([mod])
        assert "TIMEOUT" in report

    def test_skipped_report(self):
        mod = _StopModule("test", lambda: None)
        mod.skipped = True
        report = pipeline_report([mod])
        assert "SKIPPED" in report

    def test_error_report(self):
        mod = _StopModule("test", lambda: None)
        mod.error = "something broke"
        mod.elapsed_ms = 5.0
        report = pipeline_report([mod])
        assert "ERROR" in report
        assert "something broke" in report


# ── main + fallback ─────────────────────────────────────────


class TestMainEntry:
    def test_main_with_hook_data(self, tmp_path):
        """main() should not crash with minimal hook_data."""
        path = str(tmp_path / "circuit.json")
        hook_data = {"session_id": "test-123"}
        with (
            patch("cc_cortex.hooks.on_stop._CIRCUIT_STATE_PATH", path),
            patch("cc_cortex.hooks.on_stop._build_knowledge", return_value=lambda: None),
            patch("cc_cortex.hooks.on_stop._build_cognitive", return_value=lambda: None),
            patch("cc_cortex.hooks.on_stop._build_multi_instance", return_value=lambda: None),
            patch("cc_cortex.hooks.on_stop._build_stop_guard", return_value=lambda: None),
            patch("cc_cortex.hooks.on_stop._build_auto_delivery", return_value=lambda: None),
            patch("cc_cortex.hooks.on_stop._build_orphan_scan", return_value=lambda: None),
            patch("cc_cortex.hooks.on_stop._build_session_summary", return_value=lambda: None),
            patch("cc_cortex.hooks.on_stop._build_notify", return_value=lambda: None),
        ):
            from cc_cortex.hooks.on_stop import main
            main(hook_data)

    def test_main_none_reads_stdin(self):
        """main(None) tries stdin — should not crash on empty."""
        from cc_cortex.hooks.on_stop import main
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            # Should not crash even with empty hook_data
            with (
                patch("cc_cortex.hooks.on_stop._build_knowledge", return_value=lambda: None),
                patch("cc_cortex.hooks.on_stop._build_cognitive", return_value=lambda: None),
                patch("cc_cortex.hooks.on_stop._build_multi_instance", return_value=lambda: None),
                patch("cc_cortex.hooks.on_stop._build_stop_guard", return_value=lambda: None),
                patch("cc_cortex.hooks.on_stop._build_auto_delivery", return_value=lambda: None),
                patch("cc_cortex.hooks.on_stop._build_orphan_scan", return_value=lambda: None),
                patch("cc_cortex.hooks.on_stop._build_session_summary", return_value=lambda: None),
                patch("cc_cortex.hooks.on_stop._build_notify", return_value=lambda: None),
            ):
                main(None)

    def test_fallback_sequential_does_not_crash(self):
        """_fallback_sequential should be resilient."""
        with (
            patch("cc_cortex.hooks.on_stop._build_multi_instance", return_value=lambda: None),
            patch("cc_cortex.hooks.on_stop._build_notify", return_value=lambda: None),
        ):
            _fallback_sequential({"session_id": "test"})


# ── StopModule dataclass ───────────────────────────────────


class TestStopModule:
    def test_defaults(self):
        mod = _StopModule("test", lambda: None)
        assert mod.timeout_s == 10.0
        assert mod.result is None
        assert mod.error == ""
        assert mod.elapsed_ms == 0.0
        assert not mod.skipped
        assert not mod.timed_out

    def test_custom_timeout(self):
        mod = _StopModule("test", lambda: None, timeout_s=30.0)
        assert mod.timeout_s == 30.0
