"""Tests for concinno.step_back — two-tier gate buffer."""

import os

import pytest

from concinno.step_back import (
    clear_global_failures,
    clear_state,
    record_global_failure,
    wrap_gate,
)


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def sample_deny():
    return {
        "permissionDecision": "deny",
        "reason": "Test gate triggered",
    }


# ── Mode: off ─────────────────────────────────────────────

class TestModeOff:
    def test_off_returns_none(self, state_dir, sample_deny):
        result = wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="off",
        )
        assert result is None

    def test_off_does_not_write_state(self, state_dir, sample_deny):
        wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="off",
        )
        sb_dir = os.path.join(state_dir, "step_back")
        if os.path.isdir(sb_dir):
            assert len(os.listdir(sb_dir)) == 0


# ── Mode: hard_deny ─────────────────────────────────────────

class TestModeHardDeny:
    def test_always_denies(self, state_dir, sample_deny):
        result = wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="hard_deny", reason="test reason",
        )
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_preserves_original_reason(self, state_dir, sample_deny):
        result = wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="hard_deny",
        )
        assert result["reason"] == "Test gate triggered"

    def test_adds_user_visible_context(self, state_dir, sample_deny):
        result = wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="hard_deny", reason="secret key leak",
        )
        ctx = result.get("additionalContext", "")
        assert "secret key leak" in ctx
        assert "[SHOW USER VERBATIM]" in ctx

    def test_consecutive_calls_both_deny(self, state_dir, sample_deny):
        r1 = wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="hard_deny",
        )
        r2 = wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="hard_deny",
        )
        assert r1["permissionDecision"] == "deny"
        assert r2["permissionDecision"] == "deny"

    def test_backward_compat_user_reason_zh(self, state_dir, sample_deny):
        """user_reason_zh still works as deprecated alias."""
        result = wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="hard_deny", user_reason_zh="legacy reason",
        )
        ctx = result.get("additionalContext", "")
        assert "legacy reason" in ctx


# ── Mode: step_back_first ───────────────────────────────────

class TestStepBackFirst:
    def test_first_trigger_denies_with_step_back(
        self, state_dir, sample_deny,
    ):
        result = wrap_gate(
            "sentinel_gate", sample_deny, "sess1", state_dir,
            mode="step_back_first", reason="repeated edits on same file",
        )
        assert result is not None
        assert result["permissionDecision"] == "deny"
        ctx = result.get("additionalContext", "")
        assert "Stop" in ctx
        assert "repeated edits on same file" in ctx
        assert "Next identical trigger" in ctx

    def test_second_consecutive_hard_denies(
        self, state_dir, sample_deny,
    ):
        # First trigger
        wrap_gate(
            "sentinel_gate", sample_deny, "sess1", state_dir,
            mode="step_back_first", reason="repeated edits",
        )
        # Second consecutive same gate
        result = wrap_gate(
            "sentinel_gate", sample_deny, "sess1", state_dir,
            mode="step_back_first", reason="repeated edits",
        )
        assert result["permissionDecision"] == "deny"
        ctx = result.get("additionalContext", "")
        assert "consecutive trigger" in ctx
        assert "[SHOW USER VERBATIM]" in ctx

    def test_different_gate_resets(self, state_dir, sample_deny):
        # Gate A fires
        wrap_gate(
            "gate_a", sample_deny, "sess1", state_dir,
            mode="step_back_first",
        )
        # Gate B fires — should be first trigger (not consecutive)
        result = wrap_gate(
            "gate_b", sample_deny, "sess1", state_dir,
            mode="step_back_first",
        )
        ctx = result.get("additionalContext", "")
        assert "Stop" in ctx  # First trigger, not hard deny

    def test_clear_state_resets(self, state_dir, sample_deny):
        # Trigger gate
        wrap_gate(
            "sentinel_gate", sample_deny, "sess1", state_dir,
            mode="step_back_first",
        )
        # Clear state
        clear_state("sess1", state_dir)
        # Same gate again — should be first trigger
        result = wrap_gate(
            "sentinel_gate", sample_deny, "sess1", state_dir,
            mode="step_back_first",
        )
        ctx = result.get("additionalContext", "")
        assert "Stop" in ctx

    def test_after_hard_deny_resets_for_next_cycle(
        self, state_dir, sample_deny,
    ):
        # First trigger
        wrap_gate(
            "gate_x", sample_deny, "sess1", state_dir,
            mode="step_back_first",
        )
        # Second → hard deny (also resets)
        wrap_gate(
            "gate_x", sample_deny, "sess1", state_dir,
            mode="step_back_first",
        )
        # Third → should be first trigger again
        result = wrap_gate(
            "gate_x", sample_deny, "sess1", state_dir,
            mode="step_back_first",
        )
        ctx = result.get("additionalContext", "")
        assert "Stop" in ctx


# ── Invalid mode fallback ───────────────────────────────────

class TestInvalidMode:
    def test_invalid_mode_defaults_to_step_back(
        self, state_dir, sample_deny,
    ):
        result = wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="invalid_mode",
        )
        assert result is not None
        assert result["permissionDecision"] == "deny"
        ctx = result.get("additionalContext", "")
        assert "Stop" in ctx


# ── Session isolation ────────────────────────────────────────

class TestSessionIsolation:
    def test_different_sessions_independent(
        self, state_dir, sample_deny,
    ):
        # Session A triggers gate
        wrap_gate(
            "gate_x", sample_deny, "sessA", state_dir,
            mode="step_back_first",
        )
        # Session B triggers same gate — should be first trigger
        result = wrap_gate(
            "gate_x", sample_deny, "sessB", state_dir,
            mode="step_back_first",
        )
        ctx = result.get("additionalContext", "")
        assert "Stop" in ctx


# ── Reason fallback ─────────────────────────────────────────

class TestReasonFallback:
    def test_empty_reason_uses_deny_reason(
        self, state_dir, sample_deny,
    ):
        result = wrap_gate(
            "test_gate", sample_deny, "sess1", state_dir,
            mode="hard_deny", reason="",
        )
        ctx = result.get("additionalContext", "")
        assert "Test gate triggered" in ctx


# ── Global consecutive failure tracking ────────────────────


class TestGlobalFailures:
    def test_record_increments(self, state_dir):
        assert record_global_failure("s1", state_dir, "gate_a") == 1
        assert record_global_failure("s1", state_dir, "gate_b") == 2
        assert record_global_failure("s1", state_dir, "gate_a") == 3

    def test_clear_resets_count(self, state_dir):
        record_global_failure("s1", state_dir, "gate_a")
        record_global_failure("s1", state_dir, "gate_b")
        clear_global_failures("s1", state_dir)
        assert record_global_failure("s1", state_dir, "gate_c") == 1

    def test_sessions_isolated(self, state_dir):
        record_global_failure("s1", state_dir, "gate_a")
        record_global_failure("s1", state_dir, "gate_b")
        assert record_global_failure("s2", state_dir, "gate_a") == 1

    def test_wrap_gate_injects_compact_at_2_failures(
        self, state_dir, sample_deny,
    ):
        # First failure (gate_a) — normal step-back
        r1 = wrap_gate(
            "gate_a", sample_deny, "s1", state_dir,
            mode="step_back_first", reason="issue a",
        )
        ctx1 = r1.get("additionalContext", "")
        assert "/compact" not in ctx1

        # Second failure (gate_b, different gate) — compact injected
        r2 = wrap_gate(
            "gate_b", sample_deny, "s1", state_dir,
            mode="step_back_first", reason="issue b",
        )
        ctx2 = r2.get("additionalContext", "")
        assert "consecutive failures" in ctx2
        assert "/compact" in ctx2

    def test_clear_state_also_resets_global(
        self, state_dir, sample_deny,
    ):
        wrap_gate(
            "gate_a", sample_deny, "s1", state_dir,
            mode="step_back_first",
        )
        wrap_gate(
            "gate_b", sample_deny, "s1", state_dir,
            mode="step_back_first",
        )
        clear_global_failures("s1", state_dir)
        # After reset, first failure again — no compact
        r = wrap_gate(
            "gate_c", sample_deny, "s1", state_dir,
            mode="step_back_first", reason="fresh",
        )
        ctx = r.get("additionalContext", "")
        assert "/compact" not in ctx
