"""Tests for concinno.version_sync_guard."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from concinno.guards.base import GuardContext
from concinno.version_sync_guard import VersionSyncGuard


def _ctx(tool_name: str, tool_input: dict) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input,
        session_id="test",
        cache_dir="",
        hook_event="PreToolUse",
    )


def _project(tmp_path: Path, *, init_ver: str, pyproj_ver: str, changelog_ver: str) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text(
        f'__version__ = "{init_ver}"\n', encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "pkg"\nversion = "{pyproj_ver}"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{changelog_ver}] - 2026-04-17\n"
        f"- initial\n",
        encoding="utf-8",
    )
    return tmp_path


def test_allow_when_edit_does_not_touch_version(tmp_path):
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    g = VersionSyncGuard()
    res = g.check(_ctx("Edit", {
        "file_path": str(proj / "pyproject.toml"),
        "new_string": 'name = "pkg"  # cosmetic',
    }))
    assert res.action.name == "ALLOW"
    assert not res.advisory


def test_allow_when_version_matches_changelog(tmp_path):
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="2.2.0")
    g = VersionSyncGuard()
    res = g.check(_ctx("Edit", {
        "file_path": str(proj / "pyproject.toml"),
        "new_string": 'version = "2.2.0"',
    }))
    assert res.action.name == "ALLOW"
    assert not res.advisory


def test_advisory_on_drift_pyproject(tmp_path):
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    g = VersionSyncGuard()
    res = g.check(_ctx("Edit", {
        "file_path": str(proj / "pyproject.toml"),
        "new_string": 'version = "2.2.0"',
    }))
    assert res.action.name == "ALLOW"
    assert res.advisory
    assert "2.2.0" in res.context and "1.0.0" in res.context
    assert "CONCINNO_SKIP_VERSION_GATE" in res.context


def test_advisory_on_drift_dunder(tmp_path):
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    g = VersionSyncGuard()
    res = g.check(_ctx("Write", {
        "file_path": str(proj / "src" / "pkg" / "__init__.py"),
        "content": '__version__ = "2.2.0"\n',
    }))
    assert res.action.name == "ALLOW"
    assert res.advisory
    assert "2.2.0" in res.context


def test_advisory_when_no_sibling_changelog(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "1.0.0"\n', encoding="utf-8"
    )
    g = VersionSyncGuard()
    res = g.check(_ctx("Edit", {
        "file_path": str(tmp_path / "pyproject.toml"),
        "new_string": 'version = "2.2.0"',
    }))
    assert res.action.name == "ALLOW"
    assert res.advisory
    assert "no" in res.context.lower() or "CHANGELOG" in res.context


def test_env_escape_skips_check(tmp_path):
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    g = VersionSyncGuard()
    with patch.dict(os.environ, {"CONCINNO_SKIP_VERSION_GATE": "1"}):
        res = g.check(_ctx("Edit", {
            "file_path": str(proj / "pyproject.toml"),
            "new_string": 'version = "2.2.0"',
        }))
    assert res.action.name == "ALLOW"
    assert not res.advisory


def test_ignores_non_version_files(tmp_path):
    g = VersionSyncGuard()
    res = g.check(_ctx("Edit", {
        "file_path": str(tmp_path / "README.md"),
        "new_string": 'version = "2.2.0"',
    }))
    assert res.action.name == "ALLOW"
    assert not res.advisory


def test_multiedit_version_drift_detected(tmp_path):
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    g = VersionSyncGuard()
    res = g.check(_ctx("MultiEdit", {
        "file_path": str(proj / "pyproject.toml"),
        "edits": [
            {"old_string": 'version = "1.0.0"', "new_string": 'version = "2.2.0"'},
        ],
    }))
    assert res.action.name == "ALLOW"
    assert res.advisory


# ── NotebookEdit branch (Red 2 F-R2-2 fix) ────────────────
# F-R2-2: WATCHED_TOOLS listed NotebookEdit but the code had
# no explicit branch — it fell through to the MultiEdit parser
# which reads ``edits`` and NotebookEdit stores text under
# ``new_source``/``cell_source``, so a drift via NotebookEdit
# silently ALLOWed. These tests lock the fix.


def test_notebook_edit_drift_detected_via_new_source(tmp_path):
    """NotebookEdit with a version bump in ``new_source`` must warn."""
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    g = VersionSyncGuard()
    res = g.check(_ctx("NotebookEdit", {
        "file_path": str(proj / "pyproject.toml"),
        "new_source": 'version = "9.9.9"',
    }))
    assert res.action.name == "ALLOW"
    assert res.advisory  # drift detected, advisory surfaced


def test_notebook_edit_drift_detected_via_legacy_cell_source(tmp_path):
    """Legacy ``cell_source`` key is also honoured."""
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    g = VersionSyncGuard()
    res = g.check(_ctx("NotebookEdit", {
        "file_path": str(proj / "pyproject.toml"),
        "cell_source": 'version = "9.9.9"',
    }))
    assert res.action.name == "ALLOW"
    assert res.advisory


def test_notebook_edit_no_version_allows_quietly(tmp_path):
    """NotebookEdit that does not touch a version line passes without warning."""
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    g = VersionSyncGuard()
    res = g.check(_ctx("NotebookEdit", {
        "file_path": str(proj / "pyproject.toml"),
        "new_source": 'name = "pkg"  # cosmetic',
    }))
    assert res.action.name == "ALLOW"
    assert not res.advisory


def test_watched_tool_without_branch_falls_into_advisory(tmp_path, monkeypatch):
    """If a new tool is added to WATCHED_TOOLS but no branch handles it,
    the guard must surface an explicit ``unhandled write tool`` advisory
    instead of silently ALLOWing — the F-R2-2 regression pattern."""
    from concinno import version_sync_guard as vsg
    # Simulate a future expansion of WRITE_TOOLS_EXT that code hasn't
    # caught up with yet: add a tool name we guarantee has no branch.
    monkeypatch.setattr(
        vsg, "_WATCHED_TOOLS", vsg._WATCHED_TOOLS | {"SomeFutureWriteTool"},
    )
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    g = VersionSyncGuard()
    res = g.check(_ctx("SomeFutureWriteTool", {
        "file_path": str(proj / "pyproject.toml"),
        "content": 'version = "9.9.9"',
    }))
    assert res.action.name == "ALLOW"
    assert res.advisory
    assert "unhandled write tool" in (res.context or "").lower()


# ── Audit-log integrity (Red 2 H-R2-3 fix) ────────────────
# H-R2-3: H2 fix added _audit_escape JSONL logging but no test
# exercised the write path. This test covers it end-to-end.


def test_env_escape_writes_audit_log_to_cache_dir(tmp_path, monkeypatch):
    """``CONCINNO_SKIP_VERSION_GATE=1`` writes a JSONL audit record."""
    import json

    monkeypatch.setenv("CONCINNO_SKIP_VERSION_GATE", "1")
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")
    cache = tmp_path / "cache"
    cache.mkdir()

    g = VersionSyncGuard()
    ctx = GuardContext(
        tool_name="Edit",
        tool_input={
            "file_path": str(proj / "pyproject.toml"),
            "new_string": 'version = "9.9.9"',
        },
        session_id="test-session",
        cache_dir=str(cache),
        hook_event="PreToolUse",
    )
    res = g.check(ctx)
    assert res.action.name == "ALLOW"  # escape honoured

    audit = cache / "version_gate_skip.jsonl"
    assert audit.exists(), "audit log must be written at cache_dir"
    record = json.loads(audit.read_text(encoding="utf-8").strip())
    assert record["reason"] == "CONCINNO_SKIP_VERSION_GATE=1"
    assert record["session_id"] == "test-session"
    assert record["tool_name"] == "Edit"


def test_env_escape_without_cache_dir_does_not_crash(tmp_path, monkeypatch):
    """No cache_dir and no workspace — fallback to XDG ~/.cache/concinno
    path. Must not raise, must still honour the escape."""
    monkeypatch.setenv("CONCINNO_SKIP_VERSION_GATE", "1")
    proj = _project(tmp_path, init_ver="1.0.0", pyproj_ver="1.0.0", changelog_ver="1.0.0")

    g = VersionSyncGuard()
    ctx = GuardContext(
        tool_name="Edit",
        tool_input={
            "file_path": str(proj / "pyproject.toml"),
            "new_string": 'version = "9.9.9"',
        },
        session_id="",
        cache_dir="",
        hook_event="PreToolUse",
    )
    # Must not raise — the guard falls back through XDG / HOME paths.
    res = g.check(ctx)
    assert res.action.name == "ALLOW"
