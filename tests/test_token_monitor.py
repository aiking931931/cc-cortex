"""Tests for concinno.token_monitor — Token usage tracking."""

from __future__ import annotations

import json
from unittest.mock import patch

from concinno.token_monitor import (
    check_budget_gate,
    check_threshold,
    read_real_token_usage,
)

# ── read_real_token_usage ────────────────────────────────


class TestReadRealTokenUsage:
    def test_valid_usage(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        entry = {
            "message": {
                "usage": {
                    "input_tokens": 5000,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 1000,
                    "output_tokens": 500,
                }
            }
        }
        f.write_text(json.dumps(entry) + "\n")
        result = read_real_token_usage(str(f))
        assert result is not None
        assert result["context_tokens"] == 5000 + 2000 + 1000  # 8000
        assert result["output_tokens"] == 500
        assert result["cache_read_tokens"] == 2000
        # cost = 5000 + 200 + 1250 + 500 = 6950
        assert result["cost_tokens"] == 6950

    def test_no_usage_field(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        f.write_text('{"message": {"role": "assistant"}}\n')
        assert read_real_token_usage(str(f)) is None

    def test_nonexistent_file(self):
        assert read_real_token_usage("/nonexistent/file.jsonl") is None

    def test_last_entry_wins(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        old = {"message": {"usage": {"input_tokens": 100, "output_tokens": 10}}}
        new = {"message": {"usage": {"input_tokens": 9000, "output_tokens": 800,
                                      "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0}}}
        f.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
        result = read_real_token_usage(str(f))
        assert result["context_tokens"] == 9000

    def test_malformed_json(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        f.write_text("not json\n{bad\n")
        assert read_real_token_usage(str(f)) is None


# ── check_threshold ──────────────────────────────────────


class TestCheckThreshold:
    def _make_transcript(self, tmp_path, input_tokens):
        f = tmp_path / "t.jsonl"
        entry = {"message": {"usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 500,
        }}}
        f.write_text(json.dumps(entry) + "\n")
        return str(f)

    def test_below_all_thresholds(self, tmp_path):
        f = self._make_transcript(tmp_path, 5000)
        thresholds = [(100000, "🔴", "critical"), (50000, "🟡", "warning")]
        assert check_threshold(f, thresholds) is None

    def test_crosses_threshold(self, tmp_path):
        f = self._make_transcript(tmp_path, 60000)
        thresholds = [(100000, "🔴", "critical"), (50000, "🟡", "warning")]
        result = check_threshold(f, thresholds)
        assert result is not None
        assert result["threshold"] == 50000
        assert result["icon"] == "🟡"

    def test_highest_threshold_wins(self, tmp_path):
        f = self._make_transcript(tmp_path, 120000)
        thresholds = [(100000, "🔴", "critical"), (50000, "🟡", "warning")]
        result = check_threshold(f, thresholds)
        assert result["threshold"] == 100000
        assert result["icon"] == "🔴"

    def test_dedup_by_state(self, tmp_path):
        f = self._make_transcript(tmp_path, 60000)
        state_dir = str(tmp_path / "state")
        thresholds = [(50000, "🟡", "warning")]

        # First call: warns
        r1 = check_threshold(f, thresholds, state_dir=state_dir, session_id="abc12345")
        assert r1 is not None

        # Second call: deduped
        r2 = check_threshold(f, thresholds, state_dir=state_dir, session_id="abc12345")
        assert r2 is None

    def test_repeat_flag_bypasses_dedup(self, tmp_path):
        f = self._make_transcript(tmp_path, 60000)
        state_dir = str(tmp_path / "state")
        thresholds = [(50000, "🟡", "warning", True)]  # repeat=True

        r1 = check_threshold(f, thresholds, state_dir=state_dir, session_id="abc12345")
        assert r1 is not None
        r2 = check_threshold(f, thresholds, state_dir=state_dir, session_id="abc12345")
        assert r2 is not None

    def test_no_real_usage_returns_none(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("not json\n")
        thresholds = [(50000, "🟡", "warning")]
        assert check_threshold(str(f), thresholds) is None

    def test_result_fields(self, tmp_path):
        f = self._make_transcript(tmp_path, 75000)
        thresholds = [(50000, "🟡", "warning")]
        result = check_threshold(f, thresholds)
        assert "est_tokens" in result
        assert "est_k" in result
        assert "cost_k" in result
        assert result["est_k"] == 75


# ── check_budget_gate ──────────────────────────────────


class TestCheckBudgetGate:
    """Tests for the token budget PreToolUse gate."""

    def test_non_agent_always_none(self):
        """Non-Agent tools are never blocked."""
        assert check_budget_gate("sess", "Edit") is None
        assert check_budget_gate("sess", "Read") is None
        assert check_budget_gate("sess", "Write") is None
        assert check_budget_gate("sess", "Bash") is None

    def test_no_transcript_returns_none(self):
        """No transcript file → fail-open."""
        with patch(
            "concinno.token_monitor._find_transcript", return_value="",
        ):
            assert check_budget_gate("sess", "Agent") is None

    def test_no_usage_returns_none(self, tmp_path):
        """Transcript exists but no usage data → fail-open."""
        f = tmp_path / "transcript.jsonl"
        f.write_text('{"message": {}}\n')
        with patch(
            "concinno.token_monitor._find_transcript",
            return_value=str(f),
        ):
            assert check_budget_gate("sess", "Agent") is None

    def test_below_threshold_allows(self, tmp_path):
        """Token usage below agent_threshold → allowed."""
        f = tmp_path / "transcript.jsonl"
        entry = {
            "message": {
                "usage": {
                    "input_tokens": 50000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 1000,
                }
            }
        }
        f.write_text(json.dumps(entry) + "\n")
        with patch(
            "concinno.token_monitor._find_transcript",
            return_value=str(f),
        ):
            result = check_budget_gate(
                "sess", "Agent",
                agent_threshold=140000, critical_threshold=160000,
            )
            assert result is None

    def test_agent_threshold_denies(self, tmp_path):
        """Token usage at agent_threshold → deny."""
        f = tmp_path / "transcript.jsonl"
        entry = {
            "message": {
                "usage": {
                    "input_tokens": 145000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 2000,
                }
            }
        }
        f.write_text(json.dumps(entry) + "\n")
        with patch(
            "concinno.token_monitor._find_transcript",
            return_value=str(f),
        ), patch(
            "concinno.handoff_engine.get_handoff_mode",
            return_value="phase",
        ):
            result = check_budget_gate(
                "sess", "Agent",
                agent_threshold=140000, critical_threshold=160000,
            )
            assert result is not None
            assert result.get("permissionDecision") == "deny"
            assert "145K" in result.get("reason", "")

    def test_critical_threshold_denies(self, tmp_path):
        """Token usage at critical_threshold → deny with CRITICAL."""
        f = tmp_path / "transcript.jsonl"
        entry = {
            "message": {
                "usage": {
                    "input_tokens": 165000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 3000,
                }
            }
        }
        f.write_text(json.dumps(entry) + "\n")
        with patch(
            "concinno.token_monitor._find_transcript",
            return_value=str(f),
        ), patch(
            "concinno.handoff_engine.get_handoff_mode",
            return_value="phase",
        ):
            result = check_budget_gate(
                "sess", "Agent",
                agent_threshold=140000, critical_threshold=160000,
            )
            assert result is not None
            assert result.get("permissionDecision") == "deny"
            assert "CRITICAL" in result.get("reason", "")

    def test_reads_feature_config_defaults(self, tmp_path):
        """When no explicit thresholds, reads from feature_config."""
        f = tmp_path / "transcript.jsonl"
        entry = {
            "message": {
                "usage": {
                    "input_tokens": 50000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 500,
                }
            }
        }
        f.write_text(json.dumps(entry) + "\n")
        with patch(
            "concinno.token_monitor._find_transcript",
            return_value=str(f),
        ):
            # Below default 140K → should pass
            assert check_budget_gate("sess", "Agent") is None
