"""Tests for cc_cortex.handoff_claim_guard — detect claimed-but-unwritten handoffs."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

from cc_cortex.handoff_claim_guard import (
    _already_blocked,
    _extract_last_assistant_text,
    _has_handoff_claim,
    _record_block,
    on_stop,
)

# ---------------------------------------------------------------------------
# _has_handoff_claim
# ---------------------------------------------------------------------------


class TestHasHandoffClaim:
    def test_zh_claimed_written(self):
        assert _has_handoff_claim("已寫入交接") is True

    def test_zh_handoff_updated(self):
        assert _has_handoff_claim("交接已更新完畢") is True

    def test_zh_wrote_handoff(self):
        assert _has_handoff_claim("寫了交接檔") is True

    def test_zh_completed_write(self):
        assert _has_handoff_claim("完成寫入交接") is True

    def test_zh_saved_handoff(self):
        assert _has_handoff_claim("已儲存交接") is True

    def test_en_wrote_handoff(self):
        assert _has_handoff_claim("I wrote the handoff file") is True

    def test_en_handoff_updated(self):
        assert _has_handoff_claim("Handoff has been updated") is True

    def test_en_handoff_saved(self):
        assert _has_handoff_claim("handoff saved successfully") is True

    def test_arrow_pattern(self):
        assert _has_handoff_claim("→ 已寫入交接") is True

    def test_no_claim_normal_text(self):
        assert _has_handoff_claim("任務完成，結果如下") is False

    def test_no_claim_reading_handoff(self):
        assert _has_handoff_claim("讀取交接檔完成") is False

    def test_no_claim_empty(self):
        assert _has_handoff_claim("") is False

    def test_no_claim_just_handoff_word(self):
        assert _has_handoff_claim("交接") is False


# ---------------------------------------------------------------------------
# _extract_last_assistant_text
# ---------------------------------------------------------------------------


class TestExtractAssistant:
    def test_string_content(self):
        data = {"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "→ 已寫入交接"},
        ]}
        assert "交接" in _extract_last_assistant_text(data)

    def test_list_content(self):
        data = {"messages": [
            {"role": "assistant", "content": [
                {"type": "text", "text": "handoff updated"},
            ]},
        ]}
        assert "handoff" in _extract_last_assistant_text(data)

    def test_no_messages(self):
        assert _extract_last_assistant_text({}) == ""

    def test_no_assistant(self):
        data = {"messages": [{"role": "user", "content": "hi"}]}
        assert _extract_last_assistant_text(data) == ""


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_not_blocked_when_no_file(self, tmp_path):
        with patch("cc_cortex.handoff_claim_guard._BLOCK_STATE_PATH",
                    str(tmp_path / "nonexistent.json")):
            assert _already_blocked("sess1") is False

    def test_blocked_after_record(self, tmp_path):
        state_path = str(tmp_path / "block.json")
        with patch("cc_cortex.handoff_claim_guard._BLOCK_STATE_PATH", state_path):
            _record_block("sess1")
            assert _already_blocked("sess1") is True

    def test_not_blocked_different_session(self, tmp_path):
        state_path = str(tmp_path / "block.json")
        with patch("cc_cortex.handoff_claim_guard._BLOCK_STATE_PATH", state_path):
            _record_block("sess1")
            assert _already_blocked("sess2") is False

    def test_not_blocked_after_cooldown(self, tmp_path):
        state_path = str(tmp_path / "block.json")
        with patch("cc_cortex.handoff_claim_guard._BLOCK_STATE_PATH", state_path):
            # Write expired state
            with open(state_path, "w") as f:
                json.dump({"session_id": "sess1", "ts": time.time() - 400}, f)
            assert _already_blocked("sess1") is False


# ---------------------------------------------------------------------------
# on_stop integration
# ---------------------------------------------------------------------------


class TestOnStop:
    def _hook_data(self, text: str, session_id: str = "test-session") -> dict:
        return {
            "session_id": session_id,
            "messages": [
                {"role": "assistant", "content": text},
            ],
        }

    def test_no_claim_returns_none(self):
        data = self._hook_data("任務完成")
        assert on_stop(data) is None

    def test_claim_with_no_git_changes_blocks(self, tmp_path):
        """Claimed handoff but no handoff file in git → block."""
        data = self._hook_data("→ 已寫入交接", session_id="block-test")
        state_path = str(tmp_path / "block.json")
        with (
            patch("cc_cortex.handoff_claim_guard._BLOCK_STATE_PATH", state_path),
            patch("cc_cortex.handoff_claim_guard._git_changed_handoff_files", return_value=[]),
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}),
        ):
            result = on_stop(data)
            assert result is not None
            assert result.startswith("HANDOFF_CLAIM_BLOCK:")
            assert "交接聲稱已寫" in result

    def test_claim_with_git_changes_passes(self, tmp_path):
        """Claimed handoff and handoff file actually modified → pass."""
        data = self._hook_data("已寫入交接", session_id="pass-test")
        with (
            patch("cc_cortex.handoff_claim_guard._git_changed_handoff_files",
                  return_value=["_AI_BRAIN/06_Handoffs/cc-cortex/交接_CC-Cortex.md"]),
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}),
        ):
            result = on_stop(data)
            assert result is None

    def test_circuit_breaker_second_time_passes(self, tmp_path):
        """Second block attempt within cooldown → None (circuit breaker)."""
        data = self._hook_data("→ 已寫入交接", session_id="cb-test")
        state_path = str(tmp_path / "block.json")
        with (
            patch("cc_cortex.handoff_claim_guard._BLOCK_STATE_PATH", state_path),
            patch("cc_cortex.handoff_claim_guard._git_changed_handoff_files", return_value=[]),
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}),
        ):
            # First call blocks
            r1 = on_stop(data)
            assert r1 is not None
            assert r1.startswith("HANDOFF_CLAIM_BLOCK:")
            # Second call passes (circuit breaker)
            r2 = on_stop(data)
            assert r2 is None

    def test_empty_text_returns_none(self):
        data = {"session_id": "x", "messages": []}
        assert on_stop(data) is None

    def test_en_claim_blocks(self, tmp_path):
        data = self._hook_data("I wrote the handoff file", session_id="en-test")
        state_path = str(tmp_path / "block.json")
        with (
            patch("cc_cortex.handoff_claim_guard._BLOCK_STATE_PATH", state_path),
            patch("cc_cortex.handoff_claim_guard._git_changed_handoff_files", return_value=[]),
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}),
        ):
            result = on_stop(data)
            assert result is not None
            assert "HANDOFF_CLAIM_BLOCK" in result
