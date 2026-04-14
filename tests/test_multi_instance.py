"""Tests for cc_cortex.multi_instance — Multi-session coordination."""

from __future__ import annotations

from datetime import datetime, timezone

from cc_cortex.multi_instance import (
    WRITE_TOOLS,
    _is_abcd_mode,
    check_conflict,
    check_role_boundary,
    clean_same_pid_stale,
    clean_zombies,
    cleanup_marker_by_prefix,
    cleanup_role_boundary_marker,
    extract_file_path,
    is_handoff,
    is_shared,
    normalize_path,
    paths_conflict,
    register_session,
    remove_session,
    track_file,
)

# ── extract_file_path ────────────────────────────────────


class TestExtractFilePath:
    def test_read(self):
        assert extract_file_path("Read", {"file_path": "/a/b.py"}) == "/a/b.py"

    def test_write(self):
        assert extract_file_path("Write", {"file_path": "/x.py"}) == "/x.py"

    def test_edit_path_key(self):
        assert extract_file_path("Edit", {"path": "/y.py"}) == "/y.py"

    def test_notebook(self):
        assert extract_file_path("NotebookEdit", {"notebook_path": "/n.ipynb"}) == "/n.ipynb"

    def test_unknown_tool(self):
        assert extract_file_path("Bash", {"command": "ls"}) is None


# ── normalize_path ───────────────────────────────────────


class TestNormalizePath:
    def test_basic(self):
        import os
        # On Windows, normalize_path needs OS-native paths
        ws = os.path.normpath("/workspace")
        fp = os.path.join(ws, "src", "index.ts")
        result = normalize_path(fp, ws)
        assert result == "src/index.ts"

    def test_empty(self):
        assert normalize_path("", "/workspace") is None

    def test_none(self):
        assert normalize_path(None, "/workspace") is None

    def test_outside_workspace(self):
        assert normalize_path("/other/file.py", "/workspace") is None


# ── is_shared / is_handoff ───────────────────────────────


class TestIsShared:
    def test_shared_basename(self):
        shared = frozenset(["CLAUDE.md", "package.json"])
        assert is_shared("CLAUDE.md", shared, frozenset()) is True

    def test_shared_path(self):
        paths = frozenset(["config/settings.json"])
        assert is_shared("config/settings.json", frozenset(), paths) is True

    def test_not_shared(self):
        assert is_shared("src/main.py", frozenset(), frozenset()) is False

    def test_empty(self):
        assert is_shared("", frozenset(), frozenset()) is False


class TestIsHandoff:
    def test_handoff_prefix(self):
        assert is_handoff("some/dir/交接_CCC.md", ("交接_",)) is True

    def test_not_handoff(self):
        assert is_handoff("src/main.py", ("交接_",)) is False

    def test_empty(self):
        assert is_handoff("", ("交接_",)) is False


# ── paths_conflict ───────────────────────────────────────


class TestPathsConflict:
    def test_exact_match(self):
        assert paths_conflict("src/a.py", "src/a.py") is True

    def test_no_match(self):
        assert paths_conflict("src/a.py", "src/b.py") is False

    def test_glob_declared(self):
        assert paths_conflict("src/*", "src/a.py") is True

    def test_glob_target(self):
        assert paths_conflict("src/a.py", "src/*") is True

    def test_glob_no_match(self):
        assert paths_conflict("lib/*", "src/a.py") is False


# ── check_conflict ───────────────────────────────────────


class TestCheckConflict:
    def _now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    def test_no_conflict(self):
        sessions = {
            "cc_a": {"files": ["src/a.py"], "file_timestamps": {}, "holder": "A"},
        }
        assert check_conflict(sessions, "cc_b", "src/b.py") is None

    def test_conflict_found(self):
        sessions = {
            "cc_a": {
                "files": ["src/a.py"],
                "file_timestamps": {"src/a.py": self._now_iso()},
                "holder": "A",
                "task": "fix bug",
            },
        }
        result = check_conflict(sessions, "cc_b", "src/a.py")
        assert result is not None
        assert result["key"] == "cc_a"
        assert result["holder"] == "A"

    def test_stale_file_auto_released(self):
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
        sessions = {
            "cc_a": {
                "files": ["src/a.py"],
                "file_timestamps": {"src/a.py": old},
                "holder": "A",
                "task": "",
            },
        }
        assert check_conflict(sessions, "cc_b", "src/a.py") is None

    def test_skip_own_session(self):
        sessions = {
            "cc_a": {
                "files": ["src/a.py"],
                "file_timestamps": {"src/a.py": self._now_iso()},
                "holder": "A",
                "task": "",
            },
        }
        assert check_conflict(sessions, "cc_a", "src/a.py") is None


# ── register / track / remove ────────────────────────────


class TestSessionManagement:
    def test_register_new(self):
        lock = {"sessions": {}}
        register_session(lock, "cc_a", "sid123", now="2026-01-01T00:00:00")
        assert "cc_a" in lock["sessions"]
        assert lock["sessions"]["cc_a"]["session_id"] == "sid123"

    def test_register_update(self):
        lock = {"sessions": {"cc_a": {"session_id": "sid", "last_active": "old"}}}
        register_session(lock, "cc_a", "sid", now="new")
        assert lock["sessions"]["cc_a"]["last_active"] == "new"

    def test_track_file(self):
        lock = {"sessions": {"cc_a": {"files": [], "file_timestamps": {}}}}
        track_file(lock, "cc_a", "src/a.py", now="2026-01-01T00:00:00")
        assert "src/a.py" in lock["sessions"]["cc_a"]["files"]

    def test_track_file_rolling_window(self):
        lock = {"sessions": {"cc_a": {"files": [], "file_timestamps": {}}}}
        for i in range(35):
            track_file(lock, "cc_a", f"f{i}.py", max_files=30)
        assert len(lock["sessions"]["cc_a"]["files"]) == 30

    def test_track_file_nonexistent_session(self):
        lock = {"sessions": {}}
        track_file(lock, "cc_missing", "x.py")  # should not raise

    def test_remove_session(self):
        lock = {"sessions": {"cc_a": {"session_id": "x"}}}
        remove_session(lock, "cc_a")
        assert "cc_a" not in lock["sessions"]

    def test_remove_nonexistent(self):
        lock = {"sessions": {}}
        remove_session(lock, "cc_x")  # should not raise


# ── clean_zombies ────────────────────────────────────────


class TestCleanZombies:
    def test_removes_old_sessions(self):
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
        lock = {"sessions": {"cc_old": {"last_active": old, "files": []}}}
        removed = clean_zombies(lock, threshold_sec=60)
        assert "cc_old" in removed
        assert "cc_old" not in lock["sessions"]

    def test_keeps_fresh_sessions(self):
        now = datetime.now(timezone.utc).isoformat()
        lock = {"sessions": {"cc_new": {"last_active": now, "files": []}}}
        removed = clean_zombies(lock, threshold_sec=3600)
        assert removed == []
        assert "cc_new" in lock["sessions"]

    def test_abcd_mode_fast_threshold(self):
        now = datetime.now(timezone.utc).isoformat()
        lock = {"sessions": {
            f"cc_{i}": {"last_active": now, "files": [f"f{i}.py"]}
            for i in range(4)
        }}
        assert _is_abcd_mode(lock) is True

    def test_removes_invalid_timestamp(self):
        lock = {"sessions": {"cc_bad": {"last_active": "invalid", "files": []}}}
        removed = clean_zombies(lock)
        assert "cc_bad" in removed


# ── clean_same_pid_stale ────────────────────────────────


class TestCleanSamePidStale:
    def test_removes_stale_same_pid(self):
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
        lock = {"sessions": {
            "cc_a": {"vscode_pid": 1234, "last_active": old},
            "cc_b": {"vscode_pid": 1234, "last_active": datetime.now(timezone.utc).isoformat()},
        }}
        removed = clean_same_pid_stale(lock, "cc_b", 1234, stale_sec=60)
        assert "cc_a" in removed

    def test_no_pid_returns_empty(self):
        lock = {"sessions": {}}
        assert clean_same_pid_stale(lock, "cc_a", 0) == []


# ── check_role_boundary ──────────────────────────────────


class TestCheckRoleBoundary:
    def test_warns_once(self, tmp_path):
        now = datetime.now(timezone.utc).isoformat()
        lock = {"sessions": {
            "cc_a": {"last_active": now},
            "cc_b": {"last_active": now},
        }}
        marker_dir = str(tmp_path / "markers")
        warn1 = check_role_boundary(lock, "cc_a", marker_dir)
        assert warn1 is not None
        assert "1 active session" in warn1

        # Second call: marker exists, no warn
        warn2 = check_role_boundary(lock, "cc_a", marker_dir)
        assert warn2 is None

    def test_no_others_no_warn(self, tmp_path):
        now = datetime.now(timezone.utc).isoformat()
        lock = {"sessions": {"cc_a": {"last_active": now}}}
        assert check_role_boundary(lock, "cc_a", str(tmp_path)) is None


# ── cleanup_marker_by_prefix ─────────────────────────────


class TestCleanupMarkers:
    def test_cleanup(self, tmp_path):
        (tmp_path / "abc_role").write_text("1")
        (tmp_path / "abc_other").write_text("1")
        (tmp_path / "xyz_role").write_text("1")
        cleanup_marker_by_prefix(str(tmp_path), "abc")
        assert not (tmp_path / "abc_role").exists()
        assert not (tmp_path / "abc_other").exists()
        assert (tmp_path / "xyz_role").exists()

    def test_cleanup_nonexistent_dir(self):
        cleanup_marker_by_prefix("/nonexistent", "x")  # should not raise


# ── cleanup_role_boundary_marker ─────────────────────────


class TestCleanupRoleBoundaryMarker:
    def test_cleanup(self, tmp_path):
        marker = tmp_path / "abcd1234_role_boundary"
        marker.write_text("1")
        cleanup_role_boundary_marker("abcd1234-full-id", str(tmp_path))
        assert not marker.exists()

    def test_no_marker_no_error(self, tmp_path):
        cleanup_role_boundary_marker("nosuchid", str(tmp_path))


# ── WRITE_TOOLS constant ────────────────────────────────


class TestConstants:
    def test_write_tools(self):
        assert "Write" in WRITE_TOOLS
        assert "Edit" in WRITE_TOOLS
        assert "NotebookEdit" in WRITE_TOOLS
        assert "Read" not in WRITE_TOOLS
