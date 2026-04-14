"""Tests for sedimentation_gate — Block stop when corrections unsedimented."""

import json
import os
import time

from cc_cortex.core.state_store import StateStore
from cc_cortex.sedimentation_gate import (
    _find_session_corrections,
    _is_valid_feedback,
    _scan_dir_for_feedback,
    on_stop,
)

# ── _is_valid_feedback ─────────────────────────────────────────────────────


class TestIsValidFeedback:
    def test_recent_file_with_content_returns_true(self, tmp_path):
        f = tmp_path / "feedback_test.md"
        f.write_text("x" * 120)  # >= 100 chars
        cutoff = time.time() - 60
        assert _is_valid_feedback(str(f), cutoff) is True

    def test_recent_file_too_short_returns_false(self, tmp_path):
        f = tmp_path / "feedback_test.md"
        f.write_text("ok")  # < 100 chars — bypass attempt
        cutoff = time.time() - 60
        assert _is_valid_feedback(str(f), cutoff) is False

    def test_old_file_returns_false(self, tmp_path):
        f = tmp_path / "feedback_old.md"
        f.write_text("x" * 120)
        old_time = time.time() - 7200
        os.utime(str(f), (old_time, old_time))
        cutoff = time.time() - 1800
        assert _is_valid_feedback(str(f), cutoff) is False

    def test_missing_file_returns_false(self, tmp_path):
        assert _is_valid_feedback(str(tmp_path / "nonexistent.md"), 0) is False


# ── _scan_dir_for_feedback ────────────────────────────────────────────────────


class TestScanDirForFeedback:
    def test_finds_recent_feedback_file(self, tmp_path):
        f = tmp_path / "feedback_something.md"
        f.write_text("x" * 120)  # >= 100 chars to pass content check
        cutoff = time.time() - 60
        assert _scan_dir_for_feedback(str(tmp_path), cutoff) is True

    def test_ignores_non_feedback_files(self, tmp_path):
        f = tmp_path / "notes.md"
        f.write_text("content")
        cutoff = time.time() - 60
        assert _scan_dir_for_feedback(str(tmp_path), cutoff) is False

    def test_ignores_old_feedback_files(self, tmp_path):
        f = tmp_path / "feedback_old.md"
        f.write_text("content")
        old_time = time.time() - 7200
        os.utime(str(f), (old_time, old_time))
        cutoff = time.time() - 1800
        assert _scan_dir_for_feedback(str(tmp_path), cutoff) is False

    def test_missing_dir_returns_false(self, tmp_path):
        assert _scan_dir_for_feedback(str(tmp_path / "nonexistent"), 0) is False


# ── _find_session_corrections ─────────────────────────────────────────────────


class TestFindSessionCorrections:
    def test_counts_session_entries_in_jsonl(self, tmp_path, monkeypatch):
        # Patch home to tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

        queue_dir = tmp_path / ".claude" / "cognitive"
        queue_dir.mkdir(parents=True)
        queue_file = queue_dir / "corrections_queue.jsonl"

        entries = [
            {"session_id": "abc12345-extra", "correction": "fix 1"},
            {"session_id": "abc12345-other", "correction": "fix 2"},
            {"session_id": "xxxxxxxx-diff", "correction": "unrelated"},
        ]
        queue_file.write_text("\n".join(json.dumps(e) for e in entries))

        count = _find_session_corrections("abc12345")
        assert count == 2

    def test_returns_zero_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
        assert _find_session_corrections("anysession") == 0

    def test_handles_invalid_json_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

        queue_dir = tmp_path / ".claude" / "cognitive"
        queue_dir.mkdir(parents=True)
        queue_file = queue_dir / "corrections_queue.jsonl"
        queue_file.write_text('{"session_id": "abc12345"}\nNOT_JSON\n{"session_id": "abc12345"}')

        count = _find_session_corrections("abc12345")
        assert count == 2


# ── on_stop ───────────────────────────────────────────────────────────────────


class TestOnStop:
    def test_returns_none_when_no_session_id(self, monkeypatch):
        monkeypatch.delenv("CC_SESSION_ID", raising=False)
        assert on_stop({}) is None

    def test_returns_none_when_no_corrections(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_SESSION_ID", "sess-abc")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
        # No corrections_queue.jsonl → count=0
        assert on_stop({}) is None

    def test_returns_block_when_corrections_no_evidence(self, tmp_path, monkeypatch):
        session_id = "sess-deadbeef"
        monkeypatch.setenv("CC_SESSION_ID", session_id)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

        # Write corrections queue
        queue_dir = tmp_path / ".claude" / "cognitive"
        queue_dir.mkdir(parents=True)
        queue_file = queue_dir / "corrections_queue.jsonl"
        queue_file.write_text(json.dumps({"session_id": session_id, "c": "fix"}) + "\n")

        result = on_stop({})
        assert result is not None
        assert "SEDIMENTATION_BLOCK" in result

    def test_returns_none_when_evidence_exists(self, tmp_path, monkeypatch):
        session_id = "sess-evidence"
        monkeypatch.setenv("CC_SESSION_ID", session_id)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

        # Write corrections queue
        queue_dir = tmp_path / ".claude" / "cognitive"
        queue_dir.mkdir(parents=True)
        (queue_dir / "corrections_queue.jsonl").write_text(
            json.dumps({"session_id": session_id}) + "\n"
        )

        # Write recent feedback file in the projects dir
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)
        fb = projects_dir / "feedback_new_rule.md"
        fb.write_text("x" * 120)  # >= 100 chars to pass content check
        # mtime is already recent (just created)

        result = on_stop({})
        assert result is None

    def test_deadlock_breaker_after_3_blocks(self, tmp_path, monkeypatch):
        session_id = "sess-deadlock"
        monkeypatch.setenv("CC_SESSION_ID", session_id)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

        # Write corrections
        queue_dir = tmp_path / ".claude" / "cognitive"
        queue_dir.mkdir(parents=True)
        (queue_dir / "corrections_queue.jsonl").write_text(
            json.dumps({"session_id": session_id}) + "\n"
        )

        # Manually set block_count=3 (already hit deadlock threshold)
        cache_dir = tmp_path / ".cc_cortex_cache"
        cache_dir.mkdir(parents=True)
        store = StateStore(str(cache_dir))
        store.write("sedimentation_gate", session_id, {"block_count": 3})

        result = on_stop({})
        assert result is None
