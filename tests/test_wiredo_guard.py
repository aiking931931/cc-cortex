"""Tests for cc_cortex.wiredo_guard — Generalized WIREDO checklist injection."""

from __future__ import annotations

import json

from cc_cortex.asset_validator import AssetType, is_asset_type_enabled, load_wiredo_config
from cc_cortex.guards.base import GuardAction, GuardCategory, GuardContext
from cc_cortex.wiredo_guard import WiredoGuard, _build_checklist, _detect_task_type


def _make_ctx(
    tool_name: str = "Edit",
    tool_input: dict | None = None,
    workspace: str = "",
    session_id: str = "sess-1",
) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {},
        session_id=session_id,
        cache_dir="",
        hook_event="PreToolUse",
        workspace=workspace,
    )


# ── _detect_task_type ─────────────────────────────────────────


def test_detect_code_from_edit():
    ctx = _make_ctx(tool_name="Edit", tool_input={"file_path": "src/app.ts"})
    assert _detect_task_type(ctx) == AssetType.CODE


def test_detect_code_from_write():
    ctx = _make_ctx(tool_name="Write", tool_input={"file_path": "lib/utils.py"})
    assert _detect_task_type(ctx) == AssetType.CODE


def test_detect_image_from_bash_api():
    ctx = _make_ctx(tool_name="Bash", tool_input={"command": "curl fal-ai/flux-pro"})
    assert _detect_task_type(ctx) == AssetType.IMAGE


def test_detect_video_from_bash():
    ctx = _make_ctx(tool_name="Bash", tool_input={"command": "ffprobe video.mp4"})
    assert _detect_task_type(ctx) == AssetType.VIDEO


def test_detect_audio_from_bash():
    ctx = _make_ctx(tool_name="Bash", tool_input={"command": "python elevenlabs tts"})
    assert _detect_task_type(ctx) == AssetType.AUDIO


def test_detect_document_from_mcp():
    ctx = _make_ctx(tool_name="mcp__word_create", tool_input={})
    assert _detect_task_type(ctx) == AssetType.DOCUMENT


def test_detect_image_from_edit_png():
    ctx = _make_ctx(tool_name="Edit", tool_input={"file_path": "photos/avatar.png"})
    assert _detect_task_type(ctx) == AssetType.IMAGE


def test_detect_none_for_read():
    ctx = _make_ctx(tool_name="Read", tool_input={"file_path": "src/app.ts"})
    assert _detect_task_type(ctx) is None


def test_detect_none_for_grep():
    ctx = _make_ctx(tool_name="Grep", tool_input={"pattern": "test"})
    assert _detect_task_type(ctx) is None


def test_detect_none_for_plain_bash():
    ctx = _make_ctx(tool_name="Bash", tool_input={"command": "git status"})
    assert _detect_task_type(ctx) is None


# ── _build_checklist ─────────────────────────────────────────


def test_build_checklist_code():
    cl = _build_checklist(AssetType.CODE)
    assert "CODE" in cl
    assert "Wired" in cl
    assert "Observable" in cl


def test_build_checklist_image():
    cl = _build_checklist(AssetType.IMAGE)
    assert "IMAGE" in cl
    assert "800px" in cl


def test_build_checklist_video():
    cl = _build_checklist(AssetType.VIDEO)
    assert "VIDEO" in cl
    assert "2Mbps" in cl


def test_build_checklist_audio():
    cl = _build_checklist(AssetType.AUDIO)
    assert "AUDIO" in cl
    assert "LUFS" in cl


def test_build_checklist_document():
    cl = _build_checklist(AssetType.DOCUMENT)
    assert "DOCUMENT" in cl
    assert "template" in cl.lower()


# ── Config ───────────────────────────────────────────────────


def test_load_config_defaults_when_no_file(tmp_path):
    cfg = load_wiredo_config(str(tmp_path))
    assert cfg["enabled"] is True
    assert cfg["asset_types"]["code"] is True
    assert cfg["asset_types"]["image"] is True


def test_load_config_backward_compat(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo_enabled": False}), encoding="utf-8"
    )
    cfg = load_wiredo_config(str(tmp_path))
    assert cfg["enabled"] is False


def test_load_config_per_type_toggle(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo": {"asset_types": {"image": False}}}),
        encoding="utf-8",
    )
    cfg = load_wiredo_config(str(tmp_path))
    assert cfg["asset_types"]["image"] is False
    assert cfg["asset_types"]["code"] is True


def test_load_config_project_stack(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo": {"project_stack": {"psyche": ["infinite-agent"]}}}),
        encoding="utf-8",
    )
    cfg = load_wiredo_config(str(tmp_path))
    assert cfg["project_stack"]["psyche"] == ["infinite-agent"]


def test_is_asset_type_enabled_when_disabled(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo": {"asset_types": {"video": False}}}),
        encoding="utf-8",
    )
    assert is_asset_type_enabled(str(tmp_path), AssetType.VIDEO) is False
    assert is_asset_type_enabled(str(tmp_path), AssetType.CODE) is True


def test_is_asset_type_enabled_global_off(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo": {"enabled": False}}),
        encoding="utf-8",
    )
    assert is_asset_type_enabled(str(tmp_path), AssetType.CODE) is False


# ── WiredoGuard metadata ────────────────────────────────────


def test_guard_name_and_category():
    g = WiredoGuard()
    assert g.name == "wiredo"
    assert g.category == GuardCategory.COGNITIVE


# ── check() — injection per asset type ──────────────────────


def test_check_injects_on_first_code_tool(tmp_path):
    g = WiredoGuard()
    ctx = _make_ctx(tool_name="Write", tool_input={"file_path": "x.py"}, workspace=str(tmp_path))
    result = g.check(ctx)
    assert result is not None
    assert result.action == GuardAction.ALLOW
    assert "CODE" in result.context


def test_check_injects_for_edit(tmp_path):
    g = WiredoGuard()
    ctx = _make_ctx(tool_name="Edit", tool_input={"file_path": "x.ts"}, workspace=str(tmp_path))
    result = g.check(ctx)
    assert result is not None
    assert result.action == GuardAction.ALLOW


def test_check_injects_for_notebook_edit(tmp_path):
    g = WiredoGuard()
    ctx = _make_ctx(
        tool_name="NotebookEdit",
        tool_input={"file_path": "x.py"},
        workspace=str(tmp_path),
    )
    result = g.check(ctx)
    assert result is not None


# ── check() — non-relevant tools return None ─────────────────


def test_check_returns_none_for_read():
    g = WiredoGuard()
    ctx = _make_ctx(tool_name="Read")
    assert g.check(ctx) is None


def test_check_returns_none_for_plain_bash():
    g = WiredoGuard()
    ctx = _make_ctx(tool_name="Bash", tool_input={"command": "ls -la"})
    assert g.check(ctx) is None


def test_check_returns_none_for_grep():
    g = WiredoGuard()
    ctx = _make_ctx(tool_name="Grep")
    assert g.check(ctx) is None


# ── Session + type dedup ─────────────────────────────────────


def test_check_only_injects_once_per_type_per_session(tmp_path):
    g = WiredoGuard()
    ws = str(tmp_path)
    ctx1 = _make_ctx(
        tool_name="Edit", tool_input={"file_path": "x.ts"},
        workspace=ws, session_id="s1",
    )
    ctx2 = _make_ctx(
        tool_name="Write", tool_input={"file_path": "y.ts"},
        workspace=ws, session_id="s1",
    )

    result1 = g.check(ctx1)
    result2 = g.check(ctx2)

    assert result1 is not None
    assert result2 is None  # already injected CODE for this session


def test_check_injects_different_types_same_session(tmp_path):
    g = WiredoGuard()
    ws = str(tmp_path)
    ctx_code = _make_ctx(
        tool_name="Edit", tool_input={"file_path": "x.ts"},
        workspace=ws, session_id="s1",
    )
    ctx_image = _make_ctx(
        tool_name="Bash", tool_input={"command": "fal-ai/flux gen"},
        workspace=ws, session_id="s1",
    )

    r1 = g.check(ctx_code)
    r2 = g.check(ctx_image)

    assert r1 is not None
    assert r2 is not None
    assert "CODE" in r1.context
    assert "IMAGE" in r2.context


def test_check_injects_for_different_sessions(tmp_path):
    g = WiredoGuard()
    ws = str(tmp_path)
    ctx1 = _make_ctx(
        tool_name="Edit", tool_input={"file_path": "x.ts"},
        workspace=ws, session_id="s1",
    )
    ctx2 = _make_ctx(
        tool_name="Edit", tool_input={"file_path": "y.ts"},
        workspace=ws, session_id="s2",
    )

    assert g.check(ctx1) is not None
    assert g.check(ctx2) is not None


# ── Cascade note ─────────────────────────────────────────────


def test_cascade_note_for_psyche_project(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo": {"project_stack": {"psyche": ["infinite-agent"]}}}),
        encoding="utf-8",
    )
    g = WiredoGuard()
    ctx = _make_ctx(
        tool_name="Edit",
        tool_input={"file_path": "psyche-engine/console/src/App.tsx"},
        workspace=str(tmp_path),
        session_id="s1",
    )
    result = g.check(ctx)
    assert result is not None
    assert "Cascade" in result.context
    assert "infinite-agent" in result.context


# ── wiredo disabled ──────────────────────────────────────────


def test_check_disabled_returns_none(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo_enabled": False}), encoding="utf-8"
    )
    g = WiredoGuard()
    ctx = _make_ctx(tool_name="Edit", tool_input={"file_path": "x.ts"}, workspace=str(tmp_path))
    assert g.check(ctx) is None


def test_check_type_disabled_returns_none(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo": {"asset_types": {"image": False}}}),
        encoding="utf-8",
    )
    g = WiredoGuard()
    ctx = _make_ctx(
        tool_name="Bash", tool_input={"command": "fal-ai/flux gen"},
        workspace=str(tmp_path),
    )
    assert g.check(ctx) is None
