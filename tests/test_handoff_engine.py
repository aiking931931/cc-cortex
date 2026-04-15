"""Tests for handoff_engine — token gate + session summary + reminder."""

import json  # noqa: I001
import os

import pytest

from cc_cortex.handoff_engine import (
    check_handoff_reminder,
    check_token_gate,
    get_handoff_mode,
    set_handoff_mode,
    reset_handoff_reminder_state,
    HANDOFF_MODES,
)


# ── Token Gate Tests ─────────────────────────────────────────


class TestCheckTokenGate:
    """Test check_token_gate blocks Agent at high token usage."""

    @pytest.fixture(autouse=True)
    def _force_save_token_mode(self, monkeypatch):
        """Force save-token mode so gate thresholds are predictable."""
        from cc_cortex import handoff_engine
        monkeypatch.setattr(handoff_engine, "get_handoff_mode", lambda: "save-token")

    def _make_transcript(self, context_tokens: int, tmp_path) -> str:
        """Create a fake transcript JSONL with given token usage.

        Matches real Claude Code transcript shape:
          - top-level ``isSidechain`` (False for main thread)
          - ``message.role = "assistant"`` (required for read_real_token_usage)
          - ``message.usage`` with the four token fields.
        """
        p = os.path.join(str(tmp_path), "session.jsonl")
        entry = {
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "usage": {
                    "input_tokens": context_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 1000,
                },
            },
        }
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return p

    def test_allows_non_agent_tools(self):
        """Non-Agent tools should never be blocked."""
        result = check_token_gate("sess123", "Write")
        assert result is None

        result = check_token_gate("sess123", "Edit")
        assert result is None

        result = check_token_gate("sess123", "Bash")
        assert result is None

    def test_allows_agent_below_threshold(self, tmp_path, monkeypatch):
        """Agent should be allowed when tokens below gate_agent."""
        transcript = self._make_transcript(100_000, tmp_path)

        from cc_cortex import handoff_engine

        monkeypatch.setattr(
            handoff_engine, "_find_transcript", lambda sid: transcript
        )

        result = check_token_gate(
            "sess123", "Agent",
            gate_agent=140_000, gate_critical=160_000,
        )
        assert result is None

    def test_blocks_agent_at_gate(self, tmp_path, monkeypatch):
        """Agent should be DENIED when tokens >= gate_agent."""
        transcript = self._make_transcript(145_000, tmp_path)

        from cc_cortex import handoff_engine

        monkeypatch.setattr(
            handoff_engine, "_find_transcript", lambda sid: transcript
        )

        result = check_token_gate(
            "sess123", "Agent",
            gate_agent=140_000, gate_critical=160_000,
        )
        assert result is not None
        assert result["permissionDecision"] == "deny"
        assert "Token Gate" in result["reason"]
        assert "145K" in result["reason"]
        assert "additionalContext" in result

    def test_blocks_agent_critical(self, tmp_path, monkeypatch):
        """Agent should be DENIED with critical message at gate_critical."""
        transcript = self._make_transcript(165_000, tmp_path)

        from cc_cortex import handoff_engine

        monkeypatch.setattr(
            handoff_engine, "_find_transcript", lambda sid: transcript
        )

        result = check_token_gate(
            "sess123", "Agent",
            gate_agent=140_000, gate_critical=160_000,
        )
        assert result is not None
        assert result["permissionDecision"] == "deny"
        assert "🚨" in result["reason"]
        assert "CRITICAL" in result["reason"]

    def test_no_transcript_allows(self, monkeypatch):
        """If transcript not found, allow (fail-open)."""
        from cc_cortex import handoff_engine

        monkeypatch.setattr(handoff_engine, "_find_transcript", lambda sid: "")

        result = check_token_gate("sess123", "Agent")
        assert result is None

    def test_custom_thresholds(self, tmp_path, monkeypatch):
        """Custom thresholds should work."""
        transcript = self._make_transcript(50_000, tmp_path)

        from cc_cortex import handoff_engine

        monkeypatch.setattr(
            handoff_engine, "_find_transcript", lambda sid: transcript
        )

        # Default threshold (140K) — should allow
        result = check_token_gate("sess123", "Agent")
        assert result is None

        # Custom lower threshold — should block
        result = check_token_gate(
            "sess123", "Agent", gate_agent=40_000, gate_critical=80_000
        )
        assert result is not None
        assert result["permissionDecision"] == "deny"


# ── Handoff Guidance Tests ───────────────────────────────────


class TestHandoffGuidance:
    """Test handoff guidance text generation."""

    def test_zh_guidance(self, monkeypatch):
        """Chinese guidance when CC_UX_LANG=zh."""
        monkeypatch.setenv("CC_UX_LANG", "zh")
        import cc_cortex.i18n as i18n
        i18n.reload()
        from cc_cortex.handoff_engine import _handoff_guidance

        text = _handoff_guidance(142, 85, critical=False)
        assert "交接" in text or "handoff" in text.lower()
        assert "142K" in text

    def test_zh_critical(self, monkeypatch):
        """Chinese critical guidance."""
        monkeypatch.setenv("CC_UX_LANG", "zh")
        import cc_cortex.i18n as i18n
        i18n.reload()
        from cc_cortex.handoff_engine import _handoff_guidance

        text = _handoff_guidance(165, 92, critical=True)
        assert "立即" in text or "now" in text.lower()
        assert "165K" in text

    def test_en_guidance(self, monkeypatch):
        """English guidance (default)."""
        monkeypatch.setenv("CC_UX_LANG", "en")
        import cc_cortex.i18n as i18n
        i18n.reload()
        from cc_cortex.handoff_engine import _handoff_guidance

        text = _handoff_guidance(142, 85, critical=False)
        assert "handoff" in text.lower()
        assert "142K" in text

    def test_en_critical(self, monkeypatch):
        """English critical guidance."""
        monkeypatch.setenv("CC_UX_LANG", "en")
        import cc_cortex.i18n as i18n
        i18n.reload()
        from cc_cortex.handoff_engine import _handoff_guidance

        text = _handoff_guidance(165, 92, critical=True)
        assert "stop" in text.lower()
        assert "165K" in text


# ── Session Summary Tests ────────────────────────────────────


class TestSessionSummary:
    """Test session end summary generation."""

    def test_summary_with_tokens(self, tmp_path, monkeypatch):
        """Summary should include token usage."""
        transcript = os.path.join(str(tmp_path), "session.jsonl")
        entry = {
            "message": {
                "usage": {
                    "input_tokens": 85_000,
                    "cache_read_input_tokens": 40_000,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 5_000,
                }
            }
        }
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        from cc_cortex import handoff_engine

        monkeypatch.setattr(
            handoff_engine, "_find_transcript", lambda sid: transcript
        )

        summary = handoff_engine.generate_session_summary("sess123", streak=15)
        assert "125K" in summary  # 85K + 40K = 125K context
        assert "Streak: 15" in summary
        assert "╔" in summary  # Box drawing

    def test_summary_no_data(self, monkeypatch):
        """Empty summary when no data available."""
        from cc_cortex import handoff_engine

        monkeypatch.setattr(handoff_engine, "_find_transcript", lambda sid: "")

        summary = handoff_engine.generate_session_summary("sess123")
        assert summary == ""

    def test_summary_streak_labels(self, tmp_path, monkeypatch):
        """Different streak levels get different labels."""
        transcript = os.path.join(str(tmp_path), "session.jsonl")
        entry = {
            "message": {
                "usage": {
                    "input_tokens": 50_000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 1_000,
                }
            }
        }
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        from cc_cortex import handoff_engine

        monkeypatch.setattr(
            handoff_engine, "_find_transcript", lambda sid: transcript
        )

        s5 = handoff_engine.generate_session_summary("sess123", streak=5)
        assert "✨" in s5

        s10 = handoff_engine.generate_session_summary("sess123", streak=10)
        assert "solid run" in s10

        s25 = handoff_engine.generate_session_summary("sess123", streak=25)
        assert "ON FIRE" in s25


# ── Find Transcript Tests ────────────────────────────────────


class TestFindTranscript:
    """Test transcript file discovery."""

    def test_returns_empty_for_no_session(self):
        """No session_id → empty string."""
        from cc_cortex.handoff_engine import _find_transcript

        assert _find_transcript("") == ""

    def test_uses_cache(self, tmp_path, monkeypatch):
        """Should use cached path if available.

        Cache lookup in path_utils requires the cached transcript filename
        to contain the session_id (anti-cross-session safeguard), so we
        name the fake transcript ``<session_id>.jsonl``.
        """
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

        # Create cache
        cache_dir = os.path.join(str(tmp_path), ".cc_cortex_cache")
        os.makedirs(cache_dir, exist_ok=True)

        # Create fake transcript — filename must contain the session_id
        session_id = "any_session_id"
        fake_transcript = os.path.join(str(tmp_path), f"{session_id}.jsonl")
        with open(fake_transcript, "w") as f:
            f.write("{}\n")

        with open(os.path.join(cache_dir, "transcript_path.txt"), "w") as f:
            f.write(fake_transcript)

        from cc_cortex.handoff_engine import _find_transcript

        result = _find_transcript(session_id)
        assert result == fake_transcript


# ── Handoff Reminder Tests ──────────────────────────────────


class TestHandoffReminder:
    """Test check_handoff_reminder (parameter-based, no module-level file state)."""

    @pytest.fixture(autouse=True)
    def _force_save_token_mode(self, monkeypatch):
        """Force save-token mode so reminder thresholds are predictable."""
        from cc_cortex import handoff_engine
        monkeypatch.setattr(handoff_engine, "get_handoff_mode", lambda: "save-token")

    def setup_method(self):
        """Reset reminder fired state before each test."""
        reset_handoff_reminder_state()

    def test_no_reminder_below_token_threshold(self):
        """No reminder when tokens < 80K even with many files."""
        result = check_handoff_reminder(
            "sess1", 70_000, modified_count=5,
        )
        assert result is None

    def test_no_reminder_below_file_threshold(self):
        """No reminder when fewer than 3 files modified."""
        result = check_handoff_reminder(
            "sess1", 90_000, modified_count=2, token_min=80_000,
        )
        assert result is None

    def test_reminder_fires_when_conditions_met(self):
        """Reminder fires: >token_min + >=3 files + no handoff."""
        result = check_handoff_reminder(
            "sess1", 90_000, modified_count=3, token_min=80_000,
        )
        assert result is not None
        assert "90K" in result
        assert "3" in result

    def test_reminder_fires_only_once(self):
        """Same session gets reminder only once."""
        first = check_handoff_reminder(
            "sess1", 90_000, modified_count=3, token_min=80_000,
        )
        assert first is not None

        second = check_handoff_reminder(
            "sess1", 90_000, modified_count=5, token_min=80_000,
        )
        assert second is None

    def test_no_reminder_if_handoff_written(self):
        """No reminder if handoff_written=True."""
        result = check_handoff_reminder(
            "sess1", 90_000,
            modified_count=5, handoff_written=True,
        )
        assert result is None

    def test_zh_locale(self, monkeypatch):
        """Chinese locale produces Chinese reminder text."""
        monkeypatch.setenv("CC_UX_LANG", "zh")
        import cc_cortex.i18n as i18n
        i18n.reload()
        result = check_handoff_reminder(
            "sess1", 90_000, modified_count=3, token_min=80_000,
        )
        assert result is not None
        assert "交接" in result or "handoff" in result.lower()
        assert "90K" in result

    def test_en_locale(self, monkeypatch):
        """English locale produces English reminder text."""
        monkeypatch.setenv("CC_UX_LANG", "en")
        import cc_cortex.i18n as i18n
        i18n.reload()
        result = check_handoff_reminder(
            "sess1", 90_000, modified_count=4, token_min=80_000,
        )
        assert result is not None
        assert "handoff" in result.lower()

    def test_reset_clears_fired_sessions(self):
        """reset_handoff_reminder_state allows re-firing."""
        check_handoff_reminder(
            "sess1", 90_000, modified_count=3, token_min=80_000,
        )
        reset_handoff_reminder_state()
        result = check_handoff_reminder(
            "sess1", 90_000, modified_count=3, token_min=80_000,
        )
        assert result is not None

    def test_different_sessions_independent(self):
        """Different session IDs fire independently."""
        r1 = check_handoff_reminder(
            "sess1", 90_000, modified_count=3, token_min=80_000,
        )
        r2 = check_handoff_reminder(
            "sess2", 90_000, modified_count=3, token_min=80_000,
        )
        assert r1 is not None
        assert r2 is not None


# ── Handoff Mode Tests ────────────────────────────────────────


class TestHandoffMode:
    """Test handoff mode get/set and its effect on gates."""

    def test_get_mode_default(self, monkeypatch):
        """Default mode is 'phase' when config missing."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/nonexistent")
        assert get_handoff_mode() == "phase"

    def test_set_mode(self, tmp_path, monkeypatch):
        """set_handoff_mode writes to config."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cfg_dir = os.path.join(str(tmp_path), ".claude", "hooks")
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_path = os.path.join(cfg_dir, "cc_config.json")
        with open(cfg_path, "w") as f:
            json.dump({"handoff_mode": "save-token"}, f)

        assert set_handoff_mode("full")
        assert get_handoff_mode() == "full"

        assert set_handoff_mode("phase")
        assert get_handoff_mode() == "phase"

        assert not set_handoff_mode("invalid")
        assert get_handoff_mode() == "phase"  # unchanged

    def test_valid_modes(self):
        """All four modes are defined (save-token, phase, full, competition)."""
        assert HANDOFF_MODES == ("save-token", "phase", "full", "competition")

    def test_full_mode_skips_gate(self, tmp_path, monkeypatch):
        """Full mode: no token gate at all."""
        from cc_cortex import handoff_engine

        monkeypatch.setattr(handoff_engine, "get_handoff_mode", lambda: "full")

        transcript = os.path.join(str(tmp_path), "session.jsonl")
        entry = {"message": {"usage": {
            "input_tokens": 200_000, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 1000,
        }}}
        with open(transcript, "w") as f:
            f.write(json.dumps(entry) + "\n")
        monkeypatch.setattr(handoff_engine, "_find_transcript", lambda sid: transcript)

        result = check_token_gate("sess123", "Agent")
        assert result is None

    def test_phase_mode_higher_threshold(self, tmp_path, monkeypatch):
        """Phase mode: allows below phase_gate but blocks at/above it."""
        from cc_cortex import handoff_engine

        monkeypatch.setattr(handoff_engine, "get_handoff_mode", lambda: "phase")
        # Mock _model_thresholds to use testable values
        monkeypatch.setattr(handoff_engine, "_model_thresholds", lambda: {
            "gate_agent": 140_000, "gate_critical": 160_000,
            "reminder_min": 80_000,
            "phase_gate": 180_000, "phase_reminder": 150_000,
        })

        def make_transcript(tokens):
            p = os.path.join(str(tmp_path), f"session_{tokens}.jsonl")
            entry = {"message": {"usage": {
                "input_tokens": tokens, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0, "output_tokens": 1000,
            }}}
            with open(p, "w") as f:
                f.write(json.dumps(entry) + "\n")
            return p

        # 145K — allowed in phase mode
        monkeypatch.setattr(
            handoff_engine, "_find_transcript",
            lambda sid: make_transcript(145_000),
        )
        assert check_token_gate("sess123", "Agent") is None

        # 185K — blocked (safety net)
        monkeypatch.setattr(
            handoff_engine, "_find_transcript",
            lambda sid: make_transcript(185_000),
        )
        result = check_token_gate("sess123", "Agent")
        assert result is not None

    def test_full_mode_no_reminder(self, monkeypatch):
        """Full mode: no handoff reminder."""
        from cc_cortex import handoff_engine

        monkeypatch.setattr(handoff_engine, "get_handoff_mode", lambda: "full")
        reset_handoff_reminder_state()

        result = check_handoff_reminder("sess_full", 200_000, modified_count=10)
        assert result is None

    def test_phase_mode_late_reminder(self, monkeypatch):
        """Phase mode: reminder only at phase_reminder+, ignoring file count."""
        from cc_cortex import handoff_engine

        monkeypatch.setattr(handoff_engine, "get_handoff_mode", lambda: "phase")
        monkeypatch.setattr(handoff_engine, "_model_thresholds", lambda: {
            "gate_agent": 140_000, "gate_critical": 160_000,
            "reminder_min": 80_000,
            "phase_gate": 180_000, "phase_reminder": 150_000,
        })
        reset_handoff_reminder_state()

        # 100K — no reminder in phase
        r1 = check_handoff_reminder("sess_p1", 100_000, modified_count=10)
        assert r1 is None

        # 155K — reminder fires
        r2 = check_handoff_reminder("sess_p2", 155_000, modified_count=0)
        assert r2 is not None


# ── Emergency Handoff Tests ─────────────────────────────────


class TestEmergencyHandoff:
    """Test emergency_handoff writes crash snapshot."""

    def _make_handoff_dir(self, tmp_path):
        """Create a handoff directory with a sample handoff file."""
        hdir = os.path.join(str(tmp_path), "06_Handoffs", "evolution")
        os.makedirs(hdir, exist_ok=True)
        hfile = os.path.join(hdir, "交接_進化.md")
        with open(hfile, "w", encoding="utf-8") as f:
            f.write("# 交接文件：進化專案\n\n## 狀態\n\n穩定\n")
        return os.path.join(str(tmp_path), "06_Handoffs"), hfile

    def test_skips_less_than_3_files(self, tmp_path):
        """Skip if fewer than 3 modified files."""
        from cc_cortex.handoff_engine import emergency_handoff
        hdir, _ = self._make_handoff_dir(tmp_path)
        result = emergency_handoff(
            "sess1", modified_files=["a.py", "b.py"],
            reason="crash", handoff_dir=hdir,
        )
        assert result is None

    def test_skips_no_handoff_dir(self):
        """Skip if handoff_dir doesn't exist."""
        from cc_cortex.handoff_engine import emergency_handoff
        result = emergency_handoff(
            "sess1", modified_files=["a.py", "b.py", "c.py"],
            reason="crash", handoff_dir="/nonexistent",
        )
        assert result is None

    def test_writes_emergency_snippet(self, tmp_path):
        """Should append emergency snippet to best-match handoff file."""
        from cc_cortex.handoff_engine import emergency_handoff
        hdir, hfile = self._make_handoff_dir(tmp_path)
        files = ["evolution/a.py", "evolution/b.py", "evolution/c.py"]
        result = emergency_handoff(
            "sess_crash", modified_files=files,
            reason="token 耗盡", handoff_dir=hdir,
        )
        assert result is not None
        with open(result, "r", encoding="utf-8") as f:
            content = f.read()
        assert "auto-generated emergency handoff" in content
        assert "token 耗盡" in content
        assert "sess_crash" in content
        assert "evolution/a.py" in content

    def test_no_duplicate_emergency(self, tmp_path):
        """Should not stack multiple emergency handoffs."""
        from cc_cortex.handoff_engine import emergency_handoff
        hdir, _ = self._make_handoff_dir(tmp_path)
        files = ["evolution/a.py", "evolution/b.py", "evolution/c.py"]
        r1 = emergency_handoff(
            "sess1", modified_files=files,
            reason="crash", handoff_dir=hdir,
        )
        assert r1 is not None
        r2 = emergency_handoff(
            "sess2", modified_files=files,
            reason="crash again", handoff_dir=hdir,
        )
        assert r2 is None  # blocked by existing marker

    def test_caps_file_list_at_8(self, tmp_path):
        """File list in snippet should be capped at 8."""
        from cc_cortex.handoff_engine import emergency_handoff
        hdir, hfile = self._make_handoff_dir(tmp_path)
        files = [f"evolution/file_{i}.py" for i in range(15)]
        result = emergency_handoff(
            "sess1", modified_files=files,
            reason="crash", handoff_dir=hdir,
        )
        assert result is not None
        with open(result, "r", encoding="utf-8") as f:
            content = f.read()
        assert "還有 7 個" in content

    def test_fallback_to_evolution(self, tmp_path):
        """Unmatched files should fallback to evolution handoff."""
        from cc_cortex.handoff_engine import emergency_handoff
        hdir, hfile = self._make_handoff_dir(tmp_path)
        files = ["random/x.py", "random/y.py", "random/z.py"]
        result = emergency_handoff(
            "sess1", modified_files=files,
            reason="crash", handoff_dir=hdir,
        )
        assert result is not None
        assert "進化" in os.path.basename(result)


# ── Line Budget Gate Tests ──────────────────────────────────


class TestHandoffLineBudget:
    """Test check_handoff_line_budget gate."""

    def test_allows_within_budget(self, tmp_path):
        """Files within budget should pass."""
        from cc_cortex.handoff_engine import check_handoff_line_budget
        f = os.path.join(str(tmp_path), "交接_test.md")
        with open(f, "w") as fh:
            fh.write("\n".join([f"line {i}" for i in range(100)]))
        assert check_handoff_line_budget(f) is None

    def test_denies_over_budget(self, tmp_path):
        """Files over budget should be denied."""
        from cc_cortex.handoff_engine import check_handoff_line_budget
        f = os.path.join(str(tmp_path), "交接_test.md")
        with open(f, "w") as fh:
            fh.write("\n".join([f"line {i}" for i in range(350)]))
        result = check_handoff_line_budget(f)
        assert result is not None
        assert result["permissionDecision"] == "deny"
        assert "350" in result["reason"]

    def test_custom_budget(self, tmp_path):
        """Custom budget threshold should work."""
        from cc_cortex.handoff_engine import check_handoff_line_budget
        f = os.path.join(str(tmp_path), "交接_test.md")
        with open(f, "w") as fh:
            fh.write("\n".join([f"line {i}" for i in range(200)]))
        assert check_handoff_line_budget(f, budget=150) is not None
        assert check_handoff_line_budget(f, budget=250) is None

    def test_nonexistent_file(self):
        """Nonexistent file should pass (fail-open)."""
        from cc_cortex.handoff_engine import check_handoff_line_budget
        assert check_handoff_line_budget("/nonexistent/交接.md") is None


# ── Competition Mode Tests ──────────────────────────────────


class TestCompetitionMode:
    """`competition` mode is a strict superset of `full`."""

    def test_competition_in_handoff_modes_tuple(self):
        """HANDOFF_MODES exposes competition as the 4th valid mode."""
        assert "competition" in HANDOFF_MODES
        assert HANDOFF_MODES == ("save-token", "phase", "full", "competition")

    def test_is_competition_mode_detection(self, monkeypatch):
        """is_competition_mode True iff get_handoff_mode == 'competition'."""
        from cc_cortex import handoff_engine
        from cc_cortex.handoff_engine import is_competition_mode

        for mode in ("save-token", "phase", "full"):
            monkeypatch.setattr(
                handoff_engine, "get_handoff_mode", lambda m=mode: m,
            )
            assert is_competition_mode() is False, (
                f"{mode} must NOT be competition"
            )

        monkeypatch.setattr(
            handoff_engine, "get_handoff_mode", lambda: "competition",
        )
        assert is_competition_mode() is True

    def test_is_autonomous_or_competition_matches_full_and_competition(
        self, monkeypatch,
    ):
        """Convenience predicate is True for full AND competition only."""
        from cc_cortex import handoff_engine
        from cc_cortex.handoff_engine import is_autonomous_or_competition

        for mode in ("save-token", "phase"):
            monkeypatch.setattr(
                handoff_engine, "get_handoff_mode", lambda m=mode: m,
            )
            assert is_autonomous_or_competition() is False

        for mode in ("full", "competition"):
            monkeypatch.setattr(
                handoff_engine, "get_handoff_mode", lambda m=mode: m,
            )
            assert is_autonomous_or_competition() is True

    def test_competition_mode_bypasses_agent_token_gate(
        self, tmp_path, monkeypatch,
    ):
        """Competition mode skips token gating just like full mode."""
        from cc_cortex import handoff_engine

        monkeypatch.setattr(
            handoff_engine, "get_handoff_mode", lambda: "competition",
        )

        transcript = os.path.join(str(tmp_path), "session.jsonl")
        entry = {"message": {"usage": {
            "input_tokens": 200_000, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 1000,
        }}}
        with open(transcript, "w") as f:
            f.write(json.dumps(entry) + "\n")
        monkeypatch.setattr(
            handoff_engine, "_find_transcript", lambda sid: transcript,
        )

        assert check_token_gate("sess-comp", "Agent") is None

    def test_competition_mode_skips_handoff_reminder(self, monkeypatch):
        """Competition mode silences handoff reminders the same as full."""
        from cc_cortex import handoff_engine

        reset_handoff_reminder_state()
        monkeypatch.setattr(
            handoff_engine, "get_handoff_mode", lambda: "competition",
        )
        result = check_handoff_reminder(
            "sess-cr", token_usage=200_000, modified_count=10,
        )
        assert result is None

    def test_set_handoff_mode_accepts_competition(self, tmp_path, monkeypatch):
        """set_handoff_mode persists 'competition' as a valid value."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cfg_dir = os.path.join(str(tmp_path), ".claude", "hooks")
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_path = os.path.join(cfg_dir, "cc_config.json")
        with open(cfg_path, "w") as f:
            json.dump({"handoff_mode": "phase"}, f)

        assert set_handoff_mode("competition") is True
        assert get_handoff_mode() == "competition"
