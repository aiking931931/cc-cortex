"""Tests for cc_cortex.session_format — Session ID and task-pool validation."""

from __future__ import annotations

from cc_cortex.session_format import (
    SESSION_CHECK_BASENAMES,
    WRITE_TOOLS,
    _build_session_patterns,
    _get_abcd_pattern,
    check_session_id,
    check_taskpool_required,
)

# ── _build_session_patterns ──────────────────────────────


class TestBuildPatterns:
    def test_strict_matches_valid(self):
        strict, _ = _build_session_patterns("CC|IA")
        assert strict.fullmatch("CC_a3f1_0835")
        assert strict.fullmatch("IA_d9e4_2215")

    def test_strict_rejects_invalid(self):
        strict, _ = _build_session_patterns("CC|IA")
        assert strict.fullmatch("CC_toolong_0835") is None
        assert strict.fullmatch("XX_a3f1_0835") is None

    def test_loose_matches_wider(self):
        _, loose = _build_session_patterns("CC|IA")
        assert loose.findall("CC_a3f1_0835 and IA_whatever_123")


# ── _build_abcd_pattern ──────────────────────────────────


class TestABCDPattern:
    def test_detects_mother_agents(self):
        p = _get_abcd_pattern()
        assert p.search("母A — 安全強化")
        assert p.search("四母代理分工")
        assert p.search("ABCD 分派")

    def test_no_match_normal_text(self):
        p = _get_abcd_pattern()
        assert p.search("just normal text about coding") is None


# ── check_session_id ─────────────────────────────────────


class TestCheckSessionId:
    def test_non_write_tool_passes(self):
        assert check_session_id("Read", {"file_path": "task-pool.md"}) is None

    def test_non_dict_input_passes(self):
        assert check_session_id("Write", "string_input") is None

    def test_non_target_file_passes(self):
        assert check_session_id("Write", {
            "file_path": "src/main.py",
            "content": "CC_bad_format",
        }) is None

    def test_valid_session_id_passes(self):
        assert check_session_id("Write", {
            "file_path": "task-pool.md",
            "content": "CC_a3f1_0835 完成了任務",
        }) is None

    def test_invalid_session_id_blocked(self):
        result = check_session_id("Write", {
            "file_path": "task-pool.md",
            "content": "CC_toolong_value 完成了任務",
        })
        assert result is not None
        assert "format error" in result.lower() or "格式錯誤" in result

    def test_edit_tool(self):
        result = check_session_id("Edit", {
            "file_path": "task-pool.md",
            "new_string": "IA_bad 完成",
        })
        assert result is not None
        assert "IA_bad" in result

    def test_handoff_prefix(self):
        result = check_session_id("Write", {
            "file_path": "交接_CCC.md",
            "content": "PS_xyz 做完",
        }, handoff_prefixes=("交接_",))
        assert result is not None

    def test_no_session_ids_in_content(self):
        assert check_session_id("Write", {
            "file_path": "task-pool.md",
            "content": "just some text without session IDs",
        }) is None

    def test_empty_content(self):
        assert check_session_id("Write", {
            "file_path": "task-pool.md",
            "content": "",
        }) is None

    def test_notebook_edit_returns_none(self):
        """NotebookEdit is in WRITE_TOOLS but not handled for content extraction."""
        assert check_session_id("NotebookEdit", {
            "notebook_path": "task-pool.md",
        }) is None


# ── check_taskpool_required ──────────────────────────────


class TestCheckTaskpoolRequired:
    def test_non_write_tool(self):
        assert check_taskpool_required("Read", {}, ("交接_",)) is None

    def test_non_handoff_file(self):
        assert check_taskpool_required("Write", {
            "file_path": "src/main.py",
            "content": "四母代理",
        }, ("交接_",)) is None

    def test_taskpool_file_itself_skipped(self):
        assert check_taskpool_required("Write", {
            "file_path": "dir/task-pool.md",
            "content": "四母代理",
        }, ("交接_", "task-pool")) is None

    def test_abcd_with_taskpool_present(self, tmp_path):
        taskpool = tmp_path / "task-pool.md"
        taskpool.write_text("# tasks")
        handoff = tmp_path / "交接_test.md"
        assert check_taskpool_required("Write", {
            "file_path": str(handoff),
            "content": "四母代理分工如下",
        }, ("交接_",)) is None

    def test_abcd_without_taskpool_blocked(self, tmp_path):
        handoff = tmp_path / "交接_test.md"
        result = check_taskpool_required("Write", {
            "file_path": str(handoff),
            "content": "四母代理分工如下",
        }, ("交接_",))
        assert result is not None
        assert "task-pool.md" in result

    def test_no_abcd_pattern_passes(self, tmp_path):
        handoff = tmp_path / "交接_test.md"
        assert check_taskpool_required("Write", {
            "file_path": str(handoff),
            "content": "just a normal handoff update",
        }, ("交接_",)) is None

    def test_edit_tool_with_abcd(self, tmp_path):
        handoff = tmp_path / "交接_test.md"
        result = check_taskpool_required("Edit", {
            "file_path": str(handoff),
            "new_string": "ABCD 分派計畫",
        }, ("交接_",))
        assert result is not None

    def test_empty_content(self, tmp_path):
        handoff = tmp_path / "交接_test.md"
        assert check_taskpool_required("Write", {
            "file_path": str(handoff),
            "content": "",
        }, ("交接_",)) is None


# ── Constants ────────────────────────────────────────────


class TestConstants:
    def test_write_tools(self):
        assert WRITE_TOOLS == frozenset(["Write", "Edit", "NotebookEdit"])

    def test_session_check_basenames(self):
        assert "task-pool.md" in SESSION_CHECK_BASENAMES
