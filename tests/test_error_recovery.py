"""Tests for cc_cortex.error_recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cc_cortex.error_recovery import ErrorRecovery


@pytest.fixture
def recovery(tmp_path):
    return ErrorRecovery(str(tmp_path), "test-session-1234")


class TestRecordFailure:
    def test_first_failure_is_retry(self, recovery):
        level = recovery.record_failure("lint", "syntax error")
        assert level == "retry"

    def test_second_failure_still_retry(self, recovery):
        recovery.record_failure("lint", "err1")
        level = recovery.record_failure("lint", "err2")
        assert level == "retry"

    def test_third_failure_degrade(self, recovery):
        for i in range(2):
            recovery.record_failure("lint", f"err{i}")
        level = recovery.record_failure("lint", "err3")
        assert level == "degrade"

    def test_fourth_failure_degrade(self, recovery):
        for i in range(3):
            recovery.record_failure("lint", f"err{i}")
        level = recovery.record_failure("lint", "err4")
        assert level == "degrade"

    def test_fifth_failure_escalate(self, recovery):
        for i in range(4):
            recovery.record_failure("lint", f"err{i}")
        level = recovery.record_failure("lint", "err5")
        assert level == "escalate"

    def test_seventh_failure_pause(self, recovery):
        for i in range(6):
            recovery.record_failure("build", f"err{i}")
        level = recovery.record_failure("build", "err7")
        assert level == "pause"

    def test_independent_operations(self, recovery):
        recovery.record_failure("lint", "err")
        recovery.record_failure("lint", "err")
        level = recovery.record_failure("build", "err")
        assert level == "retry"  # build has only 1 failure

    def test_error_truncation(self, recovery):
        long_error = "x" * 500
        recovery.record_failure("op", long_error)
        s = recovery.status()
        assert len(s["op"]["last_error"]) <= 200


class TestRecordSuccess:
    def test_resets_count(self, recovery):
        recovery.record_failure("lint", "err1")
        recovery.record_failure("lint", "err2")
        recovery.record_success("lint")
        # Next failure should be level 1 again
        level = recovery.record_failure("lint", "err3")
        assert level == "retry"

    def test_success_on_unknown_op(self, recovery):
        # Should not crash
        recovery.record_success("nonexistent")
        assert recovery.status() == {}


class TestRecoveryAction:
    def test_all_levels(self, recovery):
        for level in ErrorRecovery.LEVELS:
            action = recovery.recovery_action(level)
            assert isinstance(action, str)
            assert len(action) > 10

    def test_unknown_level(self, recovery):
        action = recovery.recovery_action("unknown")
        assert "Stop" in action or "manual" in action


class TestStatus:
    def test_empty_status(self, recovery):
        assert recovery.status() == {}

    def test_status_after_failures(self, recovery):
        recovery.record_failure("lint", "err1")
        recovery.record_failure("build", "err2")
        s = recovery.status()
        assert "lint" in s
        assert "build" in s
        assert s["lint"]["count"] == 1
        assert s["lint"]["level"] == "retry"
        assert s["lint"]["last_error"] == "err1"

    def test_status_reflects_escalation(self, recovery):
        for i in range(5):
            recovery.record_failure("op", f"err{i}")
        s = recovery.status()
        assert s["op"]["level"] == "escalate"
        assert s["op"]["count"] == 5


# ── Burst tracking (patch-loop detection) ─────────────────


def _now() -> datetime:
    return datetime(2026, 4, 11, 20, 0, 0, tzinfo=timezone.utc)


class TestBurstTracking:
    def test_first_record_returns_1_1(self, recovery):
        total, consecutive = recovery.record_burst(
            "Bash", "timeout", now=_now(),
        )
        assert total == 1
        assert consecutive == 1

    def test_consecutive_same_pair(self, recovery):
        recovery.record_burst("Bash", "timeout", now=_now())
        total, consecutive = recovery.record_burst(
            "Bash", "timeout", now=_now() + timedelta(seconds=30),
        )
        assert total == 2
        assert consecutive == 2

    def test_reset_on_category_mismatch(self, recovery):
        t0 = _now()
        recovery.record_burst("Bash", "timeout", now=t0)
        recovery.record_burst(
            "Bash", "permission", now=t0 + timedelta(seconds=10),
        )
        total, consecutive = recovery.record_burst(
            "Bash", "timeout", now=t0 + timedelta(seconds=20),
        )
        # total timeout = 2, but consecutive stops at permission entry
        assert total == 2
        assert consecutive == 1

    def test_reset_on_operation_mismatch(self, recovery):
        t0 = _now()
        recovery.record_burst("Bash", "timeout", now=t0)
        recovery.record_burst(
            "Edit", "timeout", now=t0 + timedelta(seconds=10),
        )
        total, consecutive = recovery.record_burst(
            "Bash", "timeout", now=t0 + timedelta(seconds=20),
        )
        assert total == 2
        assert consecutive == 1  # Edit/timeout breaks the streak

    def test_window_boundary_inside(self, recovery):
        t0 = _now()
        recovery.record_burst("Bash", "timeout", now=t0)
        # 9m59s later — inside default 10-min window
        total, consecutive = recovery.record_burst(
            "Bash", "timeout",
            now=t0 + timedelta(minutes=9, seconds=59),
        )
        assert total == 2
        assert consecutive == 2

    def test_window_boundary_outside(self, recovery):
        t0 = _now()
        recovery.record_burst("Bash", "timeout", now=t0)
        # 10m01s later — outside window
        total, consecutive = recovery.record_burst(
            "Bash", "timeout",
            now=t0 + timedelta(minutes=10, seconds=1),
        )
        assert total == 2
        # Old entry is outside window → consecutive counts only new one
        assert consecutive == 1

    def test_configurable_window_shrinks_to_one_minute(self, tmp_path):
        er = ErrorRecovery(
            str(tmp_path), "burst-win-sess",
            burst_window_minutes=1,
        )
        t0 = _now()
        er.record_burst("Bash", "timeout", now=t0)
        total, consecutive = er.record_burst(
            "Bash", "timeout",
            now=t0 + timedelta(seconds=90),  # 1.5 min later
        )
        assert total == 2
        assert consecutive == 1  # outside 1-min window

    def test_history_cap_enforced(self, tmp_path):
        er = ErrorRecovery(
            str(tmp_path), "burst-cap-sess",
            burst_history_cap=50,
        )
        # Feed 250 events, all old so none are consecutive
        base = _now() - timedelta(days=1)
        for i in range(250):
            er.record_burst(
                "Bash", "other", now=base + timedelta(seconds=i),
            )
        data = er._burst_read()
        assert len(data["events"]) == 50

    def test_burst_uses_injected_now(self, recovery):
        t_custom = datetime(2020, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        recovery.record_burst("Bash", "timeout", now=t_custom)
        data = recovery._burst_read()
        assert data["events"][0]["ts"] == t_custom.isoformat()

    def test_classify_escalate_at_consecutive_two(self):
        assert ErrorRecovery.classify_burst(2, 2) == "escalate"

    def test_classify_prescribe_at_total_three(self):
        assert ErrorRecovery.classify_burst(1, 3) == "prescribe"

    def test_classify_escalate_priority_over_prescribe(self):
        assert ErrorRecovery.classify_burst(2, 5) == "escalate"

    def test_classify_normal_below_thresholds(self):
        assert ErrorRecovery.classify_burst(1, 2) == "normal"

    def test_clear_burst_all_removes_everything(self, recovery):
        recovery.record_burst("Bash", "timeout", now=_now())
        recovery.record_burst("Edit", "syntax", now=_now())
        recovery.clear_burst()
        # Next record_burst on empty history → (1, 1)
        total, consecutive = recovery.record_burst(
            "Bash", "timeout", now=_now(),
        )
        assert total == 1
        assert consecutive == 1

    def test_clear_burst_operation_scoped(self, recovery):
        recovery.record_burst("Bash", "timeout", now=_now())
        recovery.record_burst("Edit", "syntax", now=_now())
        recovery.clear_burst("Bash")
        # Edit events still present
        data = recovery._burst_read()
        assert len(data["events"]) == 1
        assert data["events"][0]["op"] == "Edit"

    def test_session_isolation(self, tmp_path):
        a = ErrorRecovery(str(tmp_path), "sess-aaaa1111")
        b = ErrorRecovery(str(tmp_path), "sess-bbbb2222")
        a.record_burst("Bash", "timeout", now=_now())
        a.record_burst("Bash", "timeout", now=_now())
        total, consecutive = b.record_burst(
            "Bash", "timeout", now=_now(),
        )
        assert total == 1
        assert consecutive == 1

    def test_burst_status_no_mutation(self, recovery):
        recovery.record_burst("Bash", "timeout", now=_now())
        s1 = recovery.burst_status("Bash", "timeout")
        s2 = recovery.burst_status("Bash", "timeout")
        assert s1 == s2
        assert s1["total"] == 1

    def test_concurrent_write_safety(self, recovery):
        # Two sequential calls on the same instance exercise
        # read_modify_write locking. Both events must persist.
        recovery.record_burst("Bash", "timeout", now=_now())
        recovery.record_burst(
            "Bash", "timeout", now=_now() + timedelta(seconds=1),
        )
        data = recovery._burst_read()
        assert len(data["events"]) == 2
