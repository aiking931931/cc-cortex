"""Tests for concinno.core.defer_loader — DeferLoader + truncation + recovery."""

from __future__ import annotations

from concinno.core.defer_loader import (
    DeferLoader,
    ModuleEntry,
    RecoveryResult,
    truncate_output,
    try_with_fallback,
)

# ── DeferLoader ──────────────────────────────────────────────


class TestDeferLoaderRegistration:
    def test_register_returns_self(self):
        loader = DeferLoader()
        result = loader.register("x", "os")
        assert result is loader

    def test_register_chaining(self):
        loader = DeferLoader()
        loader.register("a", "os").register("b", "sys")
        assert loader.total_count == 2

    def test_total_count(self):
        loader = DeferLoader()
        assert loader.total_count == 0
        loader.register("a", "os")
        assert loader.total_count == 1


class TestDeferLoaderGet:
    def test_get_stdlib_module(self):
        loader = DeferLoader()
        loader.register("os_mod", "os")
        mod = loader.get("os_mod")
        import os
        assert mod is os

    def test_get_func_from_module(self):
        loader = DeferLoader()
        loader.register("join", "os.path", func="join")
        fn = loader.get("join")
        from os.path import join
        assert fn is join

    def test_get_caches_result(self):
        loader = DeferLoader()
        loader.register("os_mod", "os")
        first = loader.get("os_mod")
        second = loader.get("os_mod")
        assert first is second

    def test_get_unknown_name(self):
        loader = DeferLoader()
        assert loader.get("nonexistent") is None

    def test_get_bad_module(self):
        loader = DeferLoader()
        loader.register("bad", "nonexistent_module_xyz_123")
        assert loader.get("bad") is None

    def test_get_bad_func(self):
        loader = DeferLoader()
        loader.register("bad", "os", func="nonexistent_func_xyz")
        assert loader.get("bad") is None

    def test_loaded_count(self):
        loader = DeferLoader()
        loader.register("a", "os")
        loader.register("b", "sys")
        assert loader.loaded_count == 0
        loader.get("a")
        assert loader.loaded_count == 1
        loader.get("b")
        assert loader.loaded_count == 2


class TestDeferLoaderFailureTracking:
    def test_failure_increments(self):
        loader = DeferLoader(max_failures=3)
        loader.register("bad", "nonexistent_xyz")
        loader.get("bad")  # fail 1
        report = loader.health_report()
        assert report["bad"]["failures"] == 1
        assert not report["bad"]["disabled"]

    def test_auto_disable_after_max_failures(self):
        loader = DeferLoader(max_failures=2)
        loader.register("bad", "nonexistent_xyz")
        loader.get("bad")  # fail 1
        loader.get("bad")  # fail 2 → disabled
        report = loader.health_report()
        assert report["bad"]["disabled"] is True
        assert report["bad"]["failures"] == 2

    def test_disabled_returns_none(self):
        loader = DeferLoader(max_failures=1)
        loader.register("bad", "nonexistent_xyz")
        loader.get("bad")  # fail → disabled
        assert loader.get("bad") is None
        assert loader.disabled_count == 1

    def test_per_module_max_failures(self):
        loader = DeferLoader(max_failures=10)
        loader.register("strict", "nonexistent_xyz", max_failures=1)
        loader.get("strict")
        assert loader.health_report()["strict"]["disabled"] is True

    def test_critical_flag_in_report(self):
        loader = DeferLoader()
        loader.register("crit", "os", critical=True)
        loader.get("crit")
        assert loader.health_report()["crit"]["critical"] is True


class TestDeferLoaderReset:
    def test_reset_re_enables(self):
        loader = DeferLoader(max_failures=1)
        loader.register("bad", "nonexistent_xyz")
        loader.get("bad")  # disabled
        assert loader.health_report()["bad"]["disabled"] is True
        loader.reset("bad")
        report = loader.health_report()
        assert report["bad"]["disabled"] is False
        assert report["bad"]["failures"] == 0

    def test_reset_unknown(self):
        loader = DeferLoader()
        assert loader.reset("nope") is False

    def test_manual_disable(self):
        loader = DeferLoader()
        loader.register("a", "os")
        loader.get("a")
        loader.disable("a")
        assert loader.get("a") is None
        assert loader.health_report()["a"]["disabled"] is True

    def test_disable_unknown(self):
        loader = DeferLoader()
        assert loader.disable("nope") is False


class TestDeferLoaderAudit:
    def test_audit_records_load(self):
        loader = DeferLoader()
        loader.register("a", "os")
        loader.get("a")
        audit = loader.audit_log()
        assert len(audit) == 1
        assert audit[0]["event"] == "loaded"
        assert audit[0]["name"] == "a"

    def test_audit_records_failure(self):
        loader = DeferLoader(max_failures=3)
        loader.register("bad", "nonexistent_xyz")
        loader.get("bad")
        audit = loader.audit_log()
        assert audit[0]["event"] == "load_failed"

    def test_audit_records_disable(self):
        loader = DeferLoader(max_failures=1)
        loader.register("bad", "nonexistent_xyz")
        loader.get("bad")
        audit = loader.audit_log()
        assert any(a["event"] == "disabled" for a in audit)

    def test_audit_records_reset(self):
        loader = DeferLoader(max_failures=1)
        loader.register("bad", "nonexistent_xyz")
        loader.get("bad")
        loader.reset("bad")
        audit = loader.audit_log()
        assert any(a["event"] == "reset" for a in audit)


class TestDeferLoaderHealthReport:
    def test_empty_report(self):
        loader = DeferLoader()
        assert loader.health_report() == {}

    def test_report_includes_load_time(self):
        loader = DeferLoader()
        loader.register("a", "os")
        loader.get("a")
        report = loader.health_report()
        assert report["a"]["load_time_ms"] >= 0

    def test_report_includes_last_error(self):
        loader = DeferLoader()
        loader.register("bad", "nonexistent_xyz")
        loader.get("bad")
        report = loader.health_report()
        assert "last_error" in report["bad"]
        assert len(report["bad"]["last_error"]) > 0


# ── truncate_output ──────────────────────────────────────────


class TestTruncateOutput:
    def test_empty(self):
        assert truncate_output("") == ""

    def test_short_text_unchanged(self):
        assert truncate_output("hello") == "hello"

    def test_truncate_by_chars(self):
        text = "a" * 100
        result = truncate_output(text, max_chars=50)
        assert len(result) == 50
        assert result.endswith("…[truncated]")

    def test_truncate_by_lines(self):
        text = "\n".join(f"line {i}" for i in range(100))
        result = truncate_output(text, max_lines=5)
        assert result.count("\n") <= 5
        assert "…[truncated]" in result

    def test_custom_suffix(self):
        text = "a" * 100
        result = truncate_output(text, max_chars=30, suffix="...")
        assert result.endswith("...")

    def test_lines_hit_before_chars(self):
        text = "\n".join(["short"] * 200)
        result = truncate_output(text, max_chars=100000, max_lines=3)
        lines = result.split("\n")
        # 3 original lines + truncated suffix
        assert len(lines) <= 5

    def test_chars_hit_before_lines(self):
        text = "x" * 5000
        result = truncate_output(text, max_chars=100, max_lines=1000)
        assert len(result) == 100

    def test_exact_limit_no_truncation(self):
        text = "hello"
        assert truncate_output(text, max_chars=5, max_lines=1) == text

    def test_none_like_input(self):
        assert truncate_output("") == ""


# ── try_with_fallback ────────────────────────────────────────


class TestTryWithFallback:
    def test_primary_succeeds(self):
        result = try_with_fallback(lambda: 42)
        assert result.succeeded is True
        assert result.value == 42
        assert result.fallback_used is False

    def test_primary_fails_fallback_succeeds(self):
        def bad():
            raise ValueError("boom")
        result = try_with_fallback(bad, fallback=lambda: 99)
        assert result.succeeded is True
        assert result.value == 99
        assert result.fallback_used is True
        assert "boom" in result.error

    def test_both_fail_returns_default(self):
        def bad1():
            raise ValueError("one")
        def bad2():
            raise ValueError("two")
        result = try_with_fallback(bad1, fallback=bad2, default="safe")
        assert result.succeeded is False
        assert result.value == "safe"
        assert "one" in result.error
        assert "two" in result.error

    def test_primary_fails_no_fallback(self):
        def bad():
            raise RuntimeError("err")
        result = try_with_fallback(bad, default=0)
        assert result.succeeded is False
        assert result.value == 0
        assert "err" in result.error

    def test_primary_returns_none(self):
        result = try_with_fallback(lambda: None)
        assert result.succeeded is True
        assert result.value is None

    def test_primary_returns_false(self):
        result = try_with_fallback(lambda: False)
        assert result.succeeded is True
        assert result.value is False


# ── ModuleEntry ──────────────────────────────────────────────


class TestModuleEntry:
    def test_defaults(self):
        entry = ModuleEntry(module_path="os")
        assert entry.func_name == ""
        assert entry.critical is False
        assert entry.max_failures == 5

    def test_custom_values(self):
        entry = ModuleEntry(
            module_path="os.path",
            func_name="join",
            critical=True,
            max_failures=3,
        )
        assert entry.func_name == "join"
        assert entry.critical is True
        assert entry.max_failures == 3


# ── RecoveryResult ───────────────────────────────────────────


class TestRecoveryResult:
    def test_success(self):
        r = RecoveryResult(succeeded=True, value=42)
        assert r.succeeded
        assert r.value == 42

    def test_failure_with_error(self):
        r = RecoveryResult(succeeded=False, error="bad")
        assert not r.succeeded
        assert r.error == "bad"


# ── Persistence (save_health / load_health) ──────────────


class TestHealthPersistence:
    def test_roundtrip_empty(self, tmp_path):
        """No failures → nothing persisted, load is no-op."""
        loader = DeferLoader()
        loader.register("os_mod", "os")
        loader.get("os_mod")  # success
        path = str(tmp_path / "health.json")
        loader.save_health(path)

        loader2 = DeferLoader()
        loader2.register("os_mod", "os")
        loader2.load_health(path)
        report = loader2.health_report()
        assert report["os_mod"]["failures"] == 0
        assert report["os_mod"]["disabled"] is False

    def test_roundtrip_with_failures(self, tmp_path):
        """Failure counts survive across processes."""
        loader = DeferLoader(max_failures=3)
        loader.register("bad", "nonexistent.module.xyz")
        loader.get("bad")  # fail 1
        loader.get("bad")  # fail 2 (still retry)
        path = str(tmp_path / "health.json")
        loader.save_health(path)

        # New process — new loader
        loader2 = DeferLoader(max_failures=3)
        loader2.register("bad", "nonexistent.module.xyz")
        loader2.load_health(path)
        report = loader2.health_report()
        assert report["bad"]["failures"] == 2
        assert report["bad"]["disabled"] is False

    def test_disabled_persists(self, tmp_path):
        """Auto-disabled modules stay disabled across processes."""
        loader = DeferLoader(max_failures=2)
        loader.register("bad", "nonexistent.module.xyz")
        loader.get("bad")  # fail 1
        loader.get("bad")  # fail 2 → disabled
        assert loader.health_report()["bad"]["disabled"] is True

        path = str(tmp_path / "health.json")
        loader.save_health(path)

        loader2 = DeferLoader(max_failures=2)
        loader2.register("bad", "nonexistent.module.xyz")
        loader2.load_health(path)
        assert loader2.health_report()["bad"]["disabled"] is True
        # get returns None without attempting import
        assert loader2.get("bad") is None

    def test_load_missing_file(self, tmp_path):
        """Missing health file → no-op (fail-open)."""
        loader = DeferLoader()
        loader.register("os_mod", "os")
        loader.load_health(str(tmp_path / "nope.json"))
        assert loader.health_report()["os_mod"]["failures"] == 0

    def test_load_corrupt_json(self, tmp_path):
        """Corrupt health file → no-op (fail-open)."""
        path = tmp_path / "health.json"
        path.write_text("not json", encoding="utf-8")
        loader = DeferLoader()
        loader.register("os_mod", "os")
        loader.load_health(str(path))
        assert loader.health_report()["os_mod"]["failures"] == 0

    def test_unknown_module_in_file_ignored(self, tmp_path):
        """Health file has entries for unregistered modules → ignored."""
        import json
        path = tmp_path / "health.json"
        path.write_text(
            json.dumps({"unknown_mod": {"failures": 5, "disabled": True}}),
            encoding="utf-8",
        )
        loader = DeferLoader()
        loader.register("os_mod", "os")
        loader.load_health(str(path))
        assert "unknown_mod" not in loader.health_report()

    def test_reset_clears_persisted_state(self, tmp_path):
        """Reset + re-save clears failure state."""
        loader = DeferLoader(max_failures=2)
        loader.register("bad", "nonexistent.module.xyz")
        loader.get("bad")
        loader.get("bad")
        path = str(tmp_path / "health.json")
        loader.save_health(path)

        loader.reset("bad")
        loader.save_health(path)

        loader2 = DeferLoader(max_failures=2)
        loader2.register("bad", "nonexistent.module.xyz")
        loader2.load_health(path)
        assert loader2.health_report()["bad"]["failures"] == 0
        assert loader2.health_report()["bad"]["disabled"] is False
