"""Tests for cc_cortex.file_tracker module."""

import json
import os

import pytest

from cc_cortex.file_tracker import FileTracker


@pytest.fixture
def tmp_env(tmp_path):
    """Create a temporary environment for FileTracker tests."""
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace)
    lock_path = os.path.join(workspace, "instance_lock.json")
    marker_dir = str(tmp_path / "markers")
    os.makedirs(marker_dir)
    return {
        "workspace": workspace,
        "lock_path": lock_path,
        "marker_dir": marker_dir,
    }


def make_tracker(tmp_env, **kwargs):
    defaults = dict(
        workspace=tmp_env["workspace"],
        lock_path=tmp_env["lock_path"],
        marker_dir=tmp_env["marker_dir"],
        shared_basenames=frozenset(["MEMORY.md"]),
        handoff_prefixes=("交接_",),
    )
    defaults.update(kwargs)
    return FileTracker(**defaults)


class TestExtractFilePath:
    """extract_file_path now delegates to core.path_utils."""

    def test_read(self):
        from cc_cortex.core.path_utils import extract_file_path
        assert extract_file_path({"file_path": "/a/b.py"}) == "/a/b.py"

    def test_write(self):
        from cc_cortex.core.path_utils import extract_file_path
        assert extract_file_path({"file_path": "/x.md"}) == "/x.md"

    def test_edit_path_key(self):
        from cc_cortex.core.path_utils import extract_file_path
        assert extract_file_path({"path": "/y.ts"}) == "/y.ts"

    def test_notebook(self):
        from cc_cortex.core.path_utils import extract_file_path
        assert extract_file_path({"notebook_path": "/n.ipynb"}) == "/n.ipynb"

    def test_unknown_tool(self):
        from cc_cortex.core.path_utils import extract_file_path
        assert extract_file_path({"command": "ls"}) == ""


class TestNormalizePath:
    def test_basic(self, tmp_env):
        t = make_tracker(tmp_env)
        ws = tmp_env["workspace"]
        fp = os.path.join(ws, "src", "index.ts")
        assert t.normalize_path(fp) == "src/index.ts"

    def test_none(self, tmp_env):
        t = make_tracker(tmp_env)
        assert t.normalize_path("") is None
        assert t.normalize_path(None) is None

    def test_outside_workspace(self, tmp_env):
        t = make_tracker(tmp_env)
        assert t.normalize_path("/completely/different/path") is None


class TestSharedAndHandoff:
    def test_shared(self, tmp_env):
        t = make_tracker(tmp_env)
        assert t.is_shared("docs/MEMORY.md") is True
        assert t.is_shared("src/main.py") is False

    def test_handoff(self, tmp_env):
        t = make_tracker(tmp_env)
        assert t.is_handoff("06_Handoffs/交接_進化.md") is True
        assert t.is_handoff("src/main.py") is False


class TestPathsConflict:
    def test_exact(self):
        assert FileTracker._paths_conflict("src/a.ts", "src/a.ts") is True

    def test_different(self):
        assert FileTracker._paths_conflict("src/a.ts", "src/b.ts") is False

    def test_glob(self):
        assert FileTracker._paths_conflict("src/*", "src/a.ts") is True
        assert FileTracker._paths_conflict("src/a.ts", "src/*") is True

    def test_glob_no_match(self):
        assert FileTracker._paths_conflict("lib/*", "src/a.ts") is False


class TestProcess:
    def test_new_session_registered(self, tmp_env):
        t = make_tracker(tmp_env)
        result = t.process("abc12345-session", "Read", {"file_path": "/x"})
        assert result["session_key"].startswith("cc_")
        assert result["is_new_session"] is True
        # Lock file should exist
        assert os.path.isfile(tmp_env["lock_path"])

    def test_write_tracked(self, tmp_env):
        t = make_tracker(tmp_env)
        ws = tmp_env["workspace"]
        fp = os.path.join(ws, "src", "main.py")
        result = t.process("sess1234-aaaa", "Edit", {"file_path": fp})
        assert "deny" not in result
        lock = json.loads(open(tmp_env["lock_path"]).read())
        sess = list(lock["sessions"].values())[0]
        assert "src/main.py" in sess["files"]

    def test_conflict_detected(self, tmp_env):
        t = make_tracker(tmp_env)
        ws = tmp_env["workspace"]
        fp = os.path.join(ws, "src", "main.py")
        # Session 1 writes
        t.process("sess1111-aaaa", "Edit", {"file_path": fp})
        # Create marker for session 1 so it's "alive"
        marker = os.path.join(tmp_env["marker_dir"], "sess1111-aaaa.active")
        with open(marker, "w") as f:
            f.write("{}")
        # Session 2 tries to write same file
        result = t.process("sess2222-bbbb", "Edit", {"file_path": fp})
        assert "deny" in result
        assert "conflict" in result["deny"]["reason"].lower()

    def test_shared_no_conflict(self, tmp_env):
        t = make_tracker(tmp_env)
        ws = tmp_env["workspace"]
        fp = os.path.join(ws, "MEMORY.md")
        t.process("sess1111-aaaa", "Edit", {"file_path": fp})
        marker = os.path.join(tmp_env["marker_dir"], "sess1111-aaaa.active")
        with open(marker, "w") as f:
            f.write("{}")
        result = t.process("sess2222-bbbb", "Edit", {"file_path": fp})
        assert "deny" not in result


class TestSessionFormat:
    """Quick integration test for session_format module."""

    def test_valid_session_id(self):
        from cc_cortex.session_format import check_session_id
        result = check_session_id(
            "Edit",
            {"file_path": "/x/task-pool.md", "new_string": "CC_a3f1_0835 做事"},
        )
        assert result is None  # valid

    def test_invalid_session_id(self):
        from cc_cortex.session_format import check_session_id
        result = check_session_id(
            "Edit",
            {"file_path": "/x/task-pool.md", "new_string": "CC_WRONG_FORMAT 做事"},
        )
        assert result is not None
        assert "format error" in result.lower()

    def test_taskpool_required(self):
        from cc_cortex.session_format import check_taskpool_required
        result = check_taskpool_required(
            "Write",
            {"file_path": "/nonexistent/交接_進化.md", "content": "母A 負責安全"},
            handoff_prefixes=("交接_",),
        )
        assert result is not None
        assert "task-pool" in result
