"""Tests for concinno.handoff_engine.auto_checkpoint.

Covers the write-action upgrade of HandoffGuard:
  - No-op when cache/handoff dirs are missing
  - Fires when token_usage >= yellow zone (model-aware)
  - Fires when modified file count >= 5
  - Skips when both triggers are below threshold
  - Idempotent per session (only fires once)
  - Block content includes token_k / next_step / changed files
  - _find_best_handoff prefers files matching path prefix
  - _find_best_handoff prefers files with next_step / ⬜ markers
  - Archived handoff files are skipped
  - Truncates file summary at 5 + "N more"
"""

from __future__ import annotations  # noqa: I001

import os
from pathlib import Path

import pytest

from concinno import handoff_engine
from concinno.handoff_engine import (
    _find_best_handoff,
    _is_archive_path,
    auto_checkpoint,
    reset_checkpoint_state,
)


# ── Helpers ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_checkpoint_state():
    """Keep _checkpoint_fired isolated between tests."""
    reset_checkpoint_state()
    yield
    reset_checkpoint_state()


@pytest.fixture
def _force_thresholds(monkeypatch):
    """Pin model thresholds so tests are deterministic across models."""
    monkeypatch.setattr(
        handoff_engine, "_model_thresholds",
        lambda: {
            "gate_agent": 140_000,
            "gate_critical": 160_000,
            "reminder_min": 80_000,
            "phase_gate": 180_000,
            "phase_reminder": 150_000,
        },
    )


def _mk_handoff(tmp_path: Path, name: str, body: str = "") -> str:
    """Create a handoff markdown file and return its path."""
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# ── 1. Guard clauses ─────────────────────────────────────────


class TestGuardClauses:
    def test_empty_handoff_dir_returns_none(self, _force_thresholds):
        result = auto_checkpoint(
            "sess",
            100_000,
            modified_files=["x.py"],
            handoff_dir="",
        )
        assert result is None

    def test_nonexistent_handoff_dir_returns_none(
        self, _force_thresholds, tmp_path,
    ):
        result = auto_checkpoint(
            "sess",
            100_000,
            modified_files=["x.py"],
            handoff_dir=str(tmp_path / "does-not-exist"),
        )
        assert result is None

    def test_below_triggers_returns_none(self, _force_thresholds, tmp_path):
        _mk_handoff(tmp_path, "交接_test.md", "⬜ next_step: foo")
        # Below yellow zone AND below 5 files
        result = auto_checkpoint(
            "sess",
            50_000,
            modified_files=["a.py", "b.py"],
            handoff_dir=str(tmp_path),
        )
        assert result is None

    def test_no_handoff_file_returns_none(self, _force_thresholds, tmp_path):
        # Meets trigger but no 交接_ file exists
        (tmp_path / "not_a_handoff.txt").write_text("hi")
        result = auto_checkpoint(
            "sess",
            100_000,
            modified_files=["x.py"],
            handoff_dir=str(tmp_path),
        )
        assert result is None


# ── 2. Trigger conditions ────────────────────────────────────


class TestTriggers:
    def test_fires_on_yellow_zone_token(self, _force_thresholds, tmp_path):
        target = _mk_handoff(tmp_path, "交接_test.md", "⬜ pending")
        result = auto_checkpoint(
            "sess",
            80_000,  # == reminder_min
            modified_files=["x.py"],
            handoff_dir=str(tmp_path),
        )
        assert result == target
        assert "auto-checkpoint" in Path(target).read_text(encoding="utf-8")

    def test_fires_on_5_file_count(self, _force_thresholds, tmp_path):
        target = _mk_handoff(tmp_path, "交接_test.md", "⬜ pending")
        result = auto_checkpoint(
            "sess",
            10_000,  # well below yellow
            modified_files=["a.py", "b.py", "c.py", "d.py", "e.py"],
            handoff_dir=str(tmp_path),
        )
        assert result == target

    def test_does_not_fire_on_4_files_below_yellow(
        self, _force_thresholds, tmp_path,
    ):
        _mk_handoff(tmp_path, "交接_test.md", "⬜ pending")
        result = auto_checkpoint(
            "sess",
            10_000,
            modified_files=["a.py", "b.py", "c.py", "d.py"],
            handoff_dir=str(tmp_path),
        )
        assert result is None


# ── 3. Idempotence ───────────────────────────────────────────


class TestIdempotence:
    def test_second_call_same_session_returns_none(
        self, _force_thresholds, tmp_path,
    ):
        _mk_handoff(tmp_path, "交接_test.md", "⬜ pending")
        first = auto_checkpoint(
            "sess",
            100_000,
            modified_files=["x.py"],
            handoff_dir=str(tmp_path),
        )
        assert first is not None

        second = auto_checkpoint(
            "sess",
            200_000,
            modified_files=["x.py"] * 10,
            handoff_dir=str(tmp_path),
        )
        assert second is None

    def test_different_sessions_both_fire(self, _force_thresholds, tmp_path):
        _mk_handoff(tmp_path, "交接_test.md", "⬜ pending")
        r1 = auto_checkpoint(
            "sess-a",
            100_000,
            modified_files=["x.py"],
            handoff_dir=str(tmp_path),
        )
        r2 = auto_checkpoint(
            "sess-b",
            100_000,
            modified_files=["y.py"],
            handoff_dir=str(tmp_path),
        )
        assert r1 is not None
        assert r2 is not None

    def test_reset_allows_refire(self, _force_thresholds, tmp_path):
        _mk_handoff(tmp_path, "交接_test.md", "⬜ pending")
        auto_checkpoint(
            "sess",
            100_000,
            modified_files=["x.py"],
            handoff_dir=str(tmp_path),
        )
        reset_checkpoint_state()
        result = auto_checkpoint(
            "sess",
            100_000,
            modified_files=["x.py"],
            handoff_dir=str(tmp_path),
        )
        assert result is not None


# ── 4. Block content ─────────────────────────────────────────


class TestBlockContent:
    def test_block_contains_token_k_and_next_step(
        self, _force_thresholds, tmp_path,
    ):
        target = _mk_handoff(tmp_path, "交接_test.md", "⬜ pending\n")
        auto_checkpoint(
            "sess",
            123_000,
            modified_files=["a.py"],
            handoff_dir=str(tmp_path),
            next_step="finish phase 3",
        )
        content = Path(target).read_text(encoding="utf-8")
        assert "ctx 123K" in content
        assert "next_step: finish phase 3" in content
        assert "a.py" in content

    def test_default_next_step_when_empty(self, _force_thresholds, tmp_path):
        target = _mk_handoff(tmp_path, "交接_test.md", "⬜ pending\n")
        auto_checkpoint(
            "sess",
            100_000,
            modified_files=["a.py"],
            handoff_dir=str(tmp_path),
        )
        content = Path(target).read_text(encoding="utf-8")
        assert "繼續未完成任務" in content

    def test_file_summary_truncates_at_5(self, _force_thresholds, tmp_path):
        target = _mk_handoff(tmp_path, "交接_test.md", "⬜ pending\n")
        files = [f"file_{i}.py" for i in range(8)]
        auto_checkpoint(
            "sess",
            100_000,
            modified_files=files,
            handoff_dir=str(tmp_path),
        )
        content = Path(target).read_text(encoding="utf-8")
        assert "file_0.py" in content
        assert "file_4.py" in content
        # file_5 and beyond are summarised as "+N more"
        assert "+3 more" in content
        assert "file_7.py" not in content

    def test_basename_only_in_summary(self, _force_thresholds, tmp_path):
        target = _mk_handoff(tmp_path, "交接_test.md", "⬜ pending\n")
        auto_checkpoint(
            "sess",
            100_000,
            modified_files=["/abs/path/to/module.py"],
            handoff_dir=str(tmp_path),
        )
        content = Path(target).read_text(encoding="utf-8")
        assert "module.py" in content
        assert "/abs/path/to/" not in content

    def test_block_appended_not_replaced(self, _force_thresholds, tmp_path):
        original = "# 交接\n\n⬜ existing work\n"
        target = _mk_handoff(tmp_path, "交接_test.md", original)
        auto_checkpoint(
            "sess",
            100_000,
            modified_files=["x.py"],
            handoff_dir=str(tmp_path),
        )
        content = Path(target).read_text(encoding="utf-8")
        assert content.startswith(original)
        assert "auto-checkpoint" in content


# ── 5. _find_best_handoff scoring ────────────────────────────


class TestFindBestHandoff:
    def test_returns_none_when_no_match(self, tmp_path):
        # File exists but has no markers and no prefix match
        _mk_handoff(tmp_path, "交接_unrelated.md", "")
        result = _find_best_handoff(str(tmp_path), [])
        assert result is None

    def test_prefers_next_step_marker(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        _mk_handoff(tmp_path / "a", "交接_A.md", "no markers")
        target = _mk_handoff(tmp_path / "b", "交接_B.md", "⬜ todo")
        # Neither path is a prefix of any modified file → score comes
        # purely from next_step/⬜ marker, which only B has.
        result = _find_best_handoff(
            str(tmp_path), [str(tmp_path / "b" / "unrelated.py")],
        )
        assert result == target

    @pytest.mark.parametrize(
        "rel,expected",
        [
            ("", False),                          # handoff_dir itself
            ("project", False),
            ("foo_archive", True),                # exact suffix match
            ("foo_archive/inside", True),         # ancestor match
            ("archive/inner", True),              # exact name match
            ("project_archived/v1", False),       # word boundary: NOT archive
            ("test_skips_archive_dir0", False),   # pytest-style: NOT archive
            ("archives/old", False),              # plural ≠ archive
        ],
    )
    def test_is_archive_path_component_match(
        self, tmp_path, rel, expected,
    ):
        """Bug 1 fix: only exact directory name suffixes count as
        archive markers. Substring match was causing pytest tmp_path
        and arbitrary `*_archived` words to be treated as archived.
        """
        target = tmp_path if rel == "" else tmp_path / rel
        assert _is_archive_path(str(target), str(tmp_path)) is expected

    def test_filter_skips_stale_dir(self, tmp_path):
        """Files under any path containing '_archive' substring are ignored.

        NOTE: the substring filter is naive — ANY ancestor dir with
        '_archive' in its name disqualifies the file. We use a nested
        `work/` dir so pytest's auto-generated tmp_path basename doesn't
        accidentally collide with the filter.
        """
        work = tmp_path / "work"
        work.mkdir()
        stale = work / "old_archive_v1"
        stale.mkdir()
        live = _mk_handoff(work, "交接_live.md", "⬜ active")
        _mk_handoff(stale, "交接_old.md", "⬜ old")

        result = _find_best_handoff(str(work), [])
        assert result == live

    def test_non_handoff_files_ignored(self, tmp_path):
        _mk_handoff(tmp_path, "notes.md", "⬜ random")
        _mk_handoff(tmp_path, "README.md", "⬜ stuff")
        result = _find_best_handoff(str(tmp_path), [])
        assert result is None

    def test_marker_beats_no_marker(self, tmp_path):
        """A handoff with ⬜/next_step always beats one without,
        since marker adds +5 and prefix matching adds only +1.
        """
        (tmp_path / "alpha").mkdir()
        (tmp_path / "bravo").mkdir()
        _mk_handoff(tmp_path / "alpha", "交接_A.md", "no markers here")
        target = _mk_handoff(tmp_path / "bravo", "交接_B.md", "⬜ beta")
        result = _find_best_handoff(str(tmp_path), [])
        assert result == target

    def test_project_tag_routes_to_correct_handoff(self, tmp_path):
        """Modified files containing the parent dir name as a path
        component score that handoff +1 per match. With markers tied,
        the project_tag tiebreaker selects the right project.
        """
        (tmp_path / "concinno").mkdir()
        (tmp_path / "aegis").mkdir()
        cortex_handoff = _mk_handoff(
            tmp_path / "concinno", "交接_CC-Cortex.md", "⬜ pending",
        )
        _mk_handoff(tmp_path / "aegis", "交接_Aegis.md", "⬜ pending")
        modified = [
            "/abs/path/projects/concinno/src/concinno/foo.py",
            "/abs/path/projects/concinno/tests/test_bar.py",
        ]
        result = _find_best_handoff(str(tmp_path), modified)
        assert result == cortex_handoff

    def test_project_tag_substring_does_not_false_positive(self, tmp_path):
        """Single-letter / substring tags must not match arbitrary
        path components. Bug 2 root cause: ``"a" in "/path/b/foo.py"``
        was True due to substring match. Component-set check fixes it.
        """
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        _mk_handoff(tmp_path / "a", "交接_A.md", "⬜ alpha")
        target = _mk_handoff(tmp_path / "b", "交接_B.md", "⬜ beta")
        # Modified file lives under "b", not "a"
        modified = [str(tmp_path / "b" / "feature.py")]
        result = _find_best_handoff(str(tmp_path), modified)
        # Both have markers (+5). Only b matches as a component (+1).
        assert result == target


# ── 6. Integration with existing handoff file ───────────────


class TestIntegration:
    def test_write_failure_returns_none(
        self, _force_thresholds, tmp_path, monkeypatch,
    ):
        _mk_handoff(tmp_path, "交接_test.md", "⬜ pending")

        original_open = open

        def broken_open(path, mode="r", *args, **kwargs):
            if "交接_test.md" in str(path) and "a" in mode:
                raise OSError("disk full")
            return original_open(path, mode, *args, **kwargs)

        # Patch the builtin open used inside auto_checkpoint
        monkeypatch.setattr("builtins.open", broken_open)

        result = auto_checkpoint(
            "sess",
            100_000,
            modified_files=["x.py"],
            handoff_dir=str(tmp_path),
        )
        assert result is None

    def test_relative_path_in_modified_files(self, _force_thresholds, tmp_path):
        """Relative paths should still produce a basename summary."""
        target = _mk_handoff(tmp_path, "交接_test.md", "⬜ pending")
        auto_checkpoint(
            "sess",
            100_000,
            modified_files=["src/concinno/foo.py"],
            handoff_dir=str(tmp_path),
        )
        content = Path(target).read_text(encoding="utf-8")
        assert "foo.py" in content
        assert os.sep + "concinno" + os.sep not in content
