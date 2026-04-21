"""Tests for concinno.sweep_guard — .git residual state warning gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno.sweep_guard import (
    RESIDUAL_MARKERS,
    check_git_residuals,
    on_stop,
)

# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect circuit-breaker state to tmp_path so tests don't leak."""
    state_path = tmp_path / "sweep_state.json"
    monkeypatch.setattr(
        "concinno.sweep_guard._STATE_PATH", state_path,
    )
    # Disable feature config reads so env/defaults govern
    monkeypatch.setenv("CONCINNO_SWEEP_SKIP", "")
    yield


@pytest.fixture
def project_with_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an empty .git directory and point CLAUDE_PROJECT_DIR there."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    return tmp_path


# ── Pure detection ────────────────────────────────────────────


class TestCheckGitResiduals:
    def test_clean_repo_returns_empty(self, project_with_git: Path):
        assert check_git_residuals(str(project_with_git)) == []

    def test_rebase_merge_detected(self, project_with_git: Path):
        (project_with_git / ".git" / "rebase-merge").mkdir()
        out = check_git_residuals(str(project_with_git))
        assert len(out) == 1
        key, label, hint = out[0]
        assert key == "rebase"
        assert "rebase-merge" in label
        assert "rebase --abort" in hint

    def test_rebase_apply_detected(self, project_with_git: Path):
        (project_with_git / ".git" / "rebase-apply").mkdir()
        out = check_git_residuals(str(project_with_git))
        assert [k for k, _, _ in out] == ["rebase"]

    def test_merge_head_detected(self, project_with_git: Path):
        (project_with_git / ".git" / "MERGE_HEAD").write_text("abc")
        out = check_git_residuals(str(project_with_git))
        assert [k for k, _, _ in out] == ["merge"]

    def test_cherry_pick_detected(self, project_with_git: Path):
        (project_with_git / ".git" / "CHERRY_PICK_HEAD").write_text("abc")
        out = check_git_residuals(str(project_with_git))
        assert [k for k, _, _ in out] == ["cherry-pick"]

    def test_revert_detected(self, project_with_git: Path):
        (project_with_git / ".git" / "REVERT_HEAD").write_text("abc")
        out = check_git_residuals(str(project_with_git))
        assert [k for k, _, _ in out] == ["revert"]

    def test_bisect_detected(self, project_with_git: Path):
        (project_with_git / ".git" / "BISECT_LOG").write_text("abc")
        out = check_git_residuals(str(project_with_git))
        assert [k for k, _, _ in out] == ["bisect"]

    def test_multiple_residuals(self, project_with_git: Path):
        (project_with_git / ".git" / "rebase-merge").mkdir()
        (project_with_git / ".git" / "MERGE_HEAD").write_text("abc")
        out = check_git_residuals(str(project_with_git))
        keys = sorted(k for k, _, _ in out)
        assert keys == ["merge", "rebase"]

    def test_non_git_dir_returns_empty(self, tmp_path: Path):
        # no .git at all
        assert check_git_residuals(str(tmp_path)) == []

    def test_linked_worktree_gitfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Worktrees have ``.git`` as a file → must follow gitdir pointer."""
        # Main repo with residual
        main = tmp_path / "main"
        main.mkdir()
        (main / ".git").mkdir()
        (main / ".git" / "rebase-merge").mkdir()
        # Linked worktree with .git file pointing to main
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(
            f"gitdir: {main / '.git'}\n", encoding="utf-8",
        )
        out = check_git_residuals(str(wt))
        assert [k for k, _, _ in out] == ["rebase"]


# ── on_stop integration ───────────────────────────────────────


class TestOnStop:
    def test_clean_session_returns_none(self, project_with_git: Path):
        assert on_stop({"session_id": "s1"}) is None

    def test_residual_returns_warning_text(self, project_with_git: Path):
        (project_with_git / ".git" / "rebase-merge").mkdir()
        out = on_stop({"session_id": "s1"})
        assert out is not None
        assert "sweep_guard" in out
        assert "rebase in progress" in out
        assert "rebase --abort" in out
        assert not out.startswith("SWEEP_BLOCK:")

    def test_block_mode_prepends_prefix(
        self,
        project_with_git: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        (project_with_git / ".git" / "MERGE_HEAD").write_text("x")
        # Stub config to return block=True
        import concinno.sweep_guard as sg

        class _FakeCfg:
            def feature(self, ns, key):
                return True if key == "block" else None

        monkeypatch.setattr(
            "concinno.core.config.get_config", lambda: _FakeCfg(),
        )
        out = sg.on_stop({"session_id": "s2"})
        assert out is not None
        assert out.startswith("SWEEP_BLOCK:")

    def test_skip_env_bypasses(
        self,
        project_with_git: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        (project_with_git / ".git" / "rebase-merge").mkdir()
        monkeypatch.setenv("CONCINNO_SWEEP_SKIP", "1")
        assert on_stop({"session_id": "s3"}) is None

    def test_feature_disabled_bypasses(
        self,
        project_with_git: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        (project_with_git / ".git" / "rebase-merge").mkdir()

        class _FakeCfg:
            def feature(self, ns, key):
                return False if key == "enabled" else None

        monkeypatch.setattr(
            "concinno.core.config.get_config", lambda: _FakeCfg(),
        )
        assert on_stop({"session_id": "s4"}) is None

    def test_circuit_breaker_suppresses_duplicate(
        self, project_with_git: Path,
    ):
        (project_with_git / ".git" / "rebase-merge").mkdir()
        first = on_stop({"session_id": "s5"})
        assert first is not None
        second = on_stop({"session_id": "s5"})
        assert second is None  # Already warned this session
        # Different session → warns again
        third = on_stop({"session_id": "s6"})
        assert third is not None

    def test_new_residual_after_warning_is_surfaced(
        self, project_with_git: Path,
    ):
        (project_with_git / ".git" / "rebase-merge").mkdir()
        first = on_stop({"session_id": "s7"})
        assert first is not None
        # Now a NEW kind of residual appears — same session should still warn
        (project_with_git / ".git" / "MERGE_HEAD").write_text("x")
        second = on_stop({"session_id": "s7"})
        assert second is not None
        assert "merge in progress" in second
        # The rebase line should NOT reappear (already acked)
        assert "rebase in progress" not in second


class TestStatePersistence:
    def test_state_file_structure(
        self, project_with_git: Path, tmp_path: Path,
    ):
        (project_with_git / ".git" / "rebase-merge").mkdir()
        on_stop({"session_id": "abc"})
        state_path = (
            __import__("concinno.sweep_guard", fromlist=["_STATE_PATH"])
            ._STATE_PATH
        )
        assert state_path.is_file()
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert "abc" in data
        assert "rebase" in data["abc"]
        assert isinstance(data["abc"]["rebase"], (int, float))


def test_residual_markers_have_hint():
    """Every marker must ship an actionable recovery hint."""
    for key, paths, hint in RESIDUAL_MARKERS:
        assert key
        assert paths
        assert hint
        # Hint should reference an actual git subcommand
        assert "git " in hint
