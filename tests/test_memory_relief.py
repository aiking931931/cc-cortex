"""Tests for concinno.memory_relief — cross-platform safe.

@module test_memory_relief
@responsibility Verify the engine + Tool + CLI surfaces work on every
    OS the rest of the suite runs on. Win32 ctypes calls are mocked so
    Linux CI exercises the same code paths the Windows host uses, just
    with the kernel boundary stubbed. The rare Windows-only assertions
    live behind ``if sys.platform == 'win32'`` guards so the file is
    importable everywhere.

Tests follow the existing pattern in ``test_process_guard.py``: small
fixture helpers, ``unittest.mock.patch`` for the OS boundary, and
plain ``assert`` (we rely on pytest, not unittest.TestCase).
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from concinno.memory_relief import core, engine
from concinno.memory_relief.engine import (
    CleanupMode,
    CleanupReport,
    PerProcessTrim,
    StageResult,
    run_cleanup,
)
from concinno.tools.builtin.memory_relief import MemoryReliefTool

# ── Snapshot dataclass shape ──────────────────────────────────────────


def test_memory_snapshot_as_dict_has_all_keys():
    """The Tool/CLI JSON output relies on a stable key set; this guards
    against accidental rename / removal of any field a downstream agent
    might be parsing."""
    snap = core.MemorySnapshot(
        total_bytes=16_000_000_000,
        available_bytes=4_000_000_000,
        used_bytes=12_000_000_000,
        standby_bytes=3_000_000_000,
        modified_bytes=100_000_000,
        commit_total_bytes=14_000_000_000,
        commit_limit_bytes=20_000_000_000,
        system_cache_bytes=2_000_000_000,
        page_size=4096,
        process_count=300,
        handle_count=80_000,
    )
    payload = snap.as_dict()
    expected_keys = {
        "total_bytes", "available_bytes", "used_bytes", "used_percent",
        "standby_bytes", "modified_bytes", "commit_total_bytes",
        "commit_limit_bytes", "commit_percent", "system_cache_bytes",
        "page_size", "process_count", "handle_count",
    }
    assert expected_keys.issubset(payload.keys())
    assert payload["used_percent"] == pytest.approx(75.0, abs=0.5)
    assert payload["commit_percent"] == pytest.approx(70.0, abs=0.5)


def test_memory_snapshot_zero_total_returns_zero_percent():
    """Edge case: empty snapshot (non-Windows path returns this) must
    not divide-by-zero on ``used_percent``."""
    snap = core.MemorySnapshot(0, 0, 0, 0, 0, 0, 0, 0, 4096, 0, 0)
    assert snap.used_percent == 0.0
    assert snap.commit_percent == 0.0


# ── CleanupMode + report serialisation ────────────────────────────────


def test_cleanup_mode_string_coercion():
    """Tool callers pass ``mode='dryrun'`` as a string; engine must
    accept that as well as the enum value."""
    report = run_cleanup(mode="dryrun", top_n=0)
    assert isinstance(report, CleanupReport)
    assert report.mode is CleanupMode.DRYRUN
    assert report.dry_run is True


def test_cleanup_mode_invalid_raises():
    with pytest.raises(ValueError, match="unknown CleanupMode"):
        run_cleanup(mode="not_a_real_mode", top_n=0)


def test_cleanup_report_as_dict_round_trip():
    """The Tool wrapper hands ``as_dict()`` straight to the agent —
    this guards the schema."""
    report = CleanupReport(
        mode=CleanupMode.SAFE,
        dry_run=True,
        is_admin=False,
        started_at=1000.0,
        finished_at=1000.5,
        before={"used_bytes": 100, "used_percent": 10.0},
        after={"used_bytes": 80, "used_percent": 8.0},
        stages=[StageResult(label="x", ok=True, elapsed_ms=42)],
        process_trims=[
            PerProcessTrim(pid=1, name="a", before_bytes=200, after_bytes=100, freed_bytes=100),
        ],
        reclaimed_bytes=20 * 1024 * 1024,
    )
    payload = report.as_dict()
    assert payload["mode"] == "safe"
    assert payload["reclaimed_mb"] == 20
    assert payload["stages"][0]["label"] == "x"
    assert payload["process_trims"][0]["freed_mb"] == 0  # 100 bytes // 1MB == 0
    assert payload["elapsed_ms"] == 500


# ── Engine dry-run end-to-end (with mocked process discovery) ─────────


def test_run_cleanup_dry_run_does_not_call_kernel(monkeypatch):
    """In ``dry_run`` every mutating wrapper must remain unreached.
    We monkeypatch each kernel-touching helper to ``boom`` and assert
    the engine still returns a complete report."""

    def boom(*_a, **_kw):
        raise AssertionError("kernel call escaped during dry_run")

    monkeypatch.setattr(core, "purge_low_priority_standby_list", boom)
    monkeypatch.setattr(core, "purge_standby_list", boom)
    monkeypatch.setattr(core, "set_system_file_cache_minimal", boom)
    monkeypatch.setattr(core, "purge_modified_page_list", boom)
    monkeypatch.setattr(core, "empty_working_set_for_pid", boom)

    report = run_cleanup(mode="aggressive", dry_run=True, top_n=2)
    assert report.dry_run is True
    # Every stage that ran should be marked skipped with reason 'dry_run'
    # (or 'needs_admin' on a non-elevated host — both are non-error).
    for stage in report.stages:
        assert stage.skipped is True
        assert stage.skip_reason in {"dry_run", "needs_admin"}
        assert stage.error == ""


def test_run_cleanup_safe_skips_admin_stages_when_not_admin(monkeypatch):
    """SAFE tier must succeed on a non-admin process. Aggressive stages
    should not even appear in the report (they are SAFE-tier exclusive
    of standby/aggressive/destructive — confirms tier gating)."""
    monkeypatch.setattr(core, "is_admin", lambda: False)
    report = run_cleanup(mode="safe", dry_run=True, top_n=0)
    assert report.is_admin is False
    stage_labels = {s.label for s in report.stages}
    # SAFE only emits the per-process trim stage.
    assert stage_labels == {"empty_working_set_per_process"}


def test_run_cleanup_aggressive_skips_admin_stages_when_not_admin(monkeypatch):
    """Aggressive tier on a non-admin host must list the admin stages
    but flag them ``skipped='needs_admin'`` — never crash, never
    silently drop them."""
    monkeypatch.setattr(core, "is_admin", lambda: False)
    report = run_cleanup(mode="aggressive", dry_run=False, top_n=0)
    assert report.is_admin is False
    admin_stages = [
        s for s in report.stages
        if s.label in {
            "purge_low_priority_standby_list",
            "purge_standby_list",
            "set_system_file_cache_minimal",
        }
    ]
    assert len(admin_stages) >= 2
    for stage in admin_stages:
        assert stage.skipped is True
        assert stage.skip_reason == "needs_admin"


# ── Tool wrapper ──────────────────────────────────────────────────────


def test_memory_relief_tool_metadata():
    """Tool Protocol requires ``name`` / ``description`` / ``call``."""
    tool = MemoryReliefTool()
    assert tool.name == "MemoryRelief"
    assert "Windows" in tool.description
    assert tool.is_concurrency_safe is False
    assert callable(tool.call)


def test_memory_relief_tool_call_returns_dict():
    """Tool returns a dict with the schema the agent contract expects."""
    tool = MemoryReliefTool()
    payload = tool.call(mode="dryrun", top_n=0)
    assert isinstance(payload, dict)
    for key in ("mode", "dry_run", "before", "after", "reclaimed_mb", "stages", "process_trims"):
        assert key in payload
    assert payload["mode"] == "dryrun"
    assert payload["dry_run"] is True


def test_memory_relief_tool_dry_run_inferred_from_mode():
    """``mode='dryrun'`` implies ``dry_run=True`` even when the caller
    leaves ``dry_run`` at its default ``None`` sentinel."""
    tool = MemoryReliefTool()
    payload = tool.call(mode="dryrun", dry_run=None, top_n=0)
    assert payload["dry_run"] is True


def test_memory_relief_tool_explicit_dry_run_override():
    """Explicit ``dry_run=False`` on a non-dryrun mode must execute
    (caveat: with ``top_n=0`` there's nothing to do, so the run is
    safe even on the test host)."""
    tool = MemoryReliefTool()
    payload = tool.call(mode="safe", dry_run=False, top_n=0)
    assert payload["dry_run"] is False


# ── FEATURE_META registration ─────────────────────────────────────────


def test_memory_relief_registered_in_feature_meta():
    """The wave-4 chain reads its tier + thresholds from FEATURE_META;
    a missing entry would silently disable the auto-trigger."""
    from concinno.feature_config import FEATURE_META

    meta = FEATURE_META.get("memory_relief")
    assert meta is not None
    assert meta["category"] == "optional_optimization"
    # Per red-team H-2: threshold must NOT be ZIQ-autotunable to dodge
    # Goodhart on disk-IO-blind outcome signal.
    assert meta["ziq_autotunable"] is False
    expected_params = {
        "auto_trigger_after_process_guard", "auto_trigger_mode",
        "top_n_per_process_trim", "min_trim_mb", "tray_enabled",
    }
    assert expected_params.issubset(meta["params"].keys())


def test_memory_relief_not_in_autotune_list():
    """Confirm ZIQ never picks memory_relief threshold for autotuning —
    the pure Goodhart guard from the red-team review."""
    from concinno.feature_config import list_autotunable

    assert "memory_relief" not in list_autotunable()


# ── process_guard wave 4 integration ──────────────────────────────────


def test_process_guard_wave4_invokes_memory_relief(monkeypatch):
    """When wave 1-3 completes and RAM is still ≥ threshold, wave 4
    must call ``run_cleanup``. We force the post-kill RAM check to be
    above threshold and assert run_cleanup was invoked once with the
    expected mode."""
    from concinno.process_guard import guard

    calls: list[dict] = []

    def fake_run_cleanup(**kwargs):
        calls.append(kwargs)
        return CleanupReport(
            mode=CleanupMode(kwargs["mode"]),
            dry_run=kwargs["dry_run"], is_admin=False,
            started_at=0.0, finished_at=0.1,
            reclaimed_bytes=42 * 1024 * 1024,
        )

    monkeypatch.setattr(guard, "_emergency_memory_relief",
                        lambda *a, **kw: ([], 0, 0))
    monkeypatch.setattr(guard, "_get_system_memory_percent", lambda: 99.0)

    with patch("concinno.memory_relief.run_cleanup", fake_run_cleanup):
        result = guard.GuardResult()
        guard._check_memory(
            claude_procs=[], all_procs=[], lock_path="",
            total_mb=0, result=result,
            memory_critical_percent=95.0, dry_run=True,
        )
    assert len(calls) == 1
    assert calls[0]["mode"] == "safe"  # default per FEATURE_META
    assert calls[0]["dry_run"] is True
    assert any("WAVE4" in a for a in result.actions)
    assert result.freed_mb == 42


def test_process_guard_wave4_skips_when_ram_recovered(monkeypatch):
    """Wave 4 must not run when wave 1-3 already brought RAM below
    threshold — the kill-then-trim ordering only escalates on need."""
    from concinno.process_guard import guard

    calls: list[dict] = []
    monkeypatch.setattr(guard, "_emergency_memory_relief",
                        lambda *a, **kw: ([], 1, 100))

    # First call (entry check) above threshold; second call (post-kill)
    # below — this is the "wave 1-3 worked" path.
    counter = {"n": 0}

    def fake_pct():
        counter["n"] += 1
        return 96.0 if counter["n"] == 1 else 80.0

    monkeypatch.setattr(guard, "_get_system_memory_percent", fake_pct)

    with patch("concinno.memory_relief.run_cleanup",
               lambda **kw: calls.append(kw)):
        result = guard.GuardResult()
        guard._check_memory(
            claude_procs=[], all_procs=[], lock_path="",
            total_mb=0, result=result,
            memory_critical_percent=95.0, dry_run=True,
        )
    assert calls == []


# ── Tray module: lazy import contract ─────────────────────────────────


def test_tray_module_imports_without_pystray():
    """The bare install (no pystray / Pillow) must still import the
    module — only :func:`main` raises ImportError, and only with the
    install-hint message."""
    from concinno.memory_relief import tray as tray_module

    assert hasattr(tray_module, "main")
    # On a host without pystray installed, _import_gui_deps raises with
    # a message pointing at the extras command.
    try:
        import pystray  # noqa: F401
        # pystray is installed; we just confirm the module exposes main.
    except ImportError:
        with pytest.raises(ImportError, match="memory-relief-tray"):
            tray_module._import_gui_deps()


# ── Windows-only smoke (only runs when pytest is on Windows) ──────────


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only smoke")
def test_real_snapshot_on_windows():
    """A live ``GetPerformanceInfo`` call should yield a non-zero
    total_bytes — guards against accidental no-op shims."""
    snap = core.get_memory_snapshot()
    assert snap.total_bytes > 0
    assert snap.process_count > 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only smoke")
def test_real_dry_run_on_windows():
    """End-to-end dry-run on the test host. Must not raise even when
    not admin (admin-only stages skip with ``needs_admin``)."""
    report = engine.run_cleanup(mode="dryrun", top_n=2)
    assert report.dry_run is True
    assert report.before
    assert report.after
