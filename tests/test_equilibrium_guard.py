"""Tests for cc_cortex.equilibrium_guard — write-and-clean for handoff files."""

from __future__ import annotations

import os
import textwrap

import pytest

from cc_cortex.equilibrium_guard import (
    KEEP_RECENT,
    EquilibriumGuard,
    _atomic_rewrite,
    _find_recent_section,
    _is_handoff_file,
    _parse_records,
    cleanup_handoff,
)
from cc_cortex.guards.base import GuardContext

# ── Fixtures ─────────────────────────────────────────────────────

SAMPLE_HANDOFF = textwrap.dedent("""\
    ---
    status: active
    verified: true
    last_updated: 2026-03-20
    ---
    # 交接：Test Project

    ## 🔴 必讀區

    - **狀態**：✅ All done

    ## 🟡 按需區

    ### 近期記錄（僅保留最後 3 筆）

    - 03-21a：✅ **Session A**（100 tests）
      - ✅ Sub item A1
      - ✅ Sub item A2
    - 03-20b：✅ **Session B**（90 tests）
      - ✅ Sub item B1
    - 03-19c：✅ **Session C**（80 tests）
      - ✅ Sub item C1
      - ✅ Sub item C2
      - ✅ Sub item C3
    - 03-18d：✅ **Session D**（70 tests）
      - ✅ Sub item D1
    - 03-17e：✅ **Session E**（60 tests）
      - ✅ Sub item E1

    ## 🔵 邊界
""")

SAMPLE_EXACTLY_3 = textwrap.dedent("""\
    ---
    status: active
    ---
    # 交接：Exact

    ### 近期記錄（僅保留最後 3 筆）

    - 03-21a：✅ **A**
    - 03-20b：✅ **B**
    - 03-19c：✅ **C**

    ## Next
""")

SAMPLE_NO_SECTION = textwrap.dedent("""\
    ---
    status: active
    ---
    # 交接：No Recent

    ## 🔴 必讀區

    - Done
""")


# ── Unit: _is_handoff_file ───────────────────────────────────────


class TestIsHandoffFile:
    def test_chinese_prefix(self):
        assert _is_handoff_file("path/to/交接_foo.md") is True

    def test_english_prefix(self):
        assert _is_handoff_file("path/to/handoff_bar.md") is True

    def test_not_handoff(self):
        assert _is_handoff_file("path/to/readme.md") is False

    def test_not_markdown(self):
        assert _is_handoff_file("path/to/交接_foo.py") is False

    def test_empty(self):
        assert _is_handoff_file("") is False


# ── Unit: _find_recent_section ───────────────────────────────────


class TestFindRecentSection:
    def test_finds_section(self):
        lines = SAMPLE_HANDOFF.split("\n")
        start, end = _find_recent_section(lines)
        assert start > 0
        assert end > start
        assert "03-21a" in lines[start]

    def test_no_section(self):
        lines = SAMPLE_NO_SECTION.split("\n")
        start, end = _find_recent_section(lines)
        assert start == -1
        assert end == -1


# ── Unit: _parse_records ─────────────────────────────────────────


class TestParseRecords:
    def test_parses_5_records(self):
        lines = SAMPLE_HANDOFF.split("\n")
        start, end = _find_recent_section(lines)
        records = _parse_records(lines, start, end)
        assert len(records) == 5

    def test_parses_3_records(self):
        lines = SAMPLE_EXACTLY_3.split("\n")
        start, end = _find_recent_section(lines)
        records = _parse_records(lines, start, end)
        assert len(records) == 3

    def test_records_ordered(self):
        lines = SAMPLE_HANDOFF.split("\n")
        start, end = _find_recent_section(lines)
        records = _parse_records(lines, start, end)
        # First record should be 03-21a
        assert "03-21a" in lines[records[0][0]]
        # Last record should be 03-17e
        assert "03-17e" in lines[records[-1][0]]


# ── Unit: cleanup_handoff ────────────────────────────────────────


class TestCleanupHandoff:
    def test_trims_to_3(self):
        cleaned, removed = cleanup_handoff(SAMPLE_HANDOFF)
        assert removed == 2
        assert "03-21a" in cleaned
        assert "03-20b" in cleaned
        assert "03-19c" in cleaned
        assert "03-18d" not in cleaned
        assert "03-17e" not in cleaned

    def test_no_change_when_at_limit(self):
        cleaned, removed = cleanup_handoff(SAMPLE_EXACTLY_3)
        assert removed == 0
        assert cleaned == SAMPLE_EXACTLY_3

    def test_no_section_no_change(self):
        cleaned, removed = cleanup_handoff(SAMPLE_NO_SECTION)
        assert removed == 0

    def test_preserves_frontmatter(self):
        cleaned, _ = cleanup_handoff(SAMPLE_HANDOFF)
        assert "status: active" in cleaned
        assert "verified: true" in cleaned

    def test_preserves_sections_after(self):
        cleaned, _ = cleanup_handoff(SAMPLE_HANDOFF)
        assert "## 🔵 邊界" in cleaned

    def test_custom_keep(self):
        cleaned, removed = cleanup_handoff(SAMPLE_HANDOFF, keep=1)
        assert removed == 4
        assert "03-21a" in cleaned
        assert "03-20b" not in cleaned

    def test_sub_items_removed_with_parent(self):
        cleaned, _ = cleanup_handoff(SAMPLE_HANDOFF)
        assert "Sub item D1" not in cleaned
        assert "Sub item E1" not in cleaned
        # Kept records' sub-items preserved
        assert "Sub item A1" in cleaned
        assert "Sub item C3" in cleaned


# ── Unit: _atomic_rewrite ────────────────────────────────────────


class TestAtomicRewrite:
    def test_writes_content(self, tmp_path):
        target = str(tmp_path / "test.md")
        with open(target, "w") as f:
            f.write("old")
        _atomic_rewrite(target, "new content")
        with open(target) as f:
            assert f.read() == "new content"

    def test_no_leftover_tmp(self, tmp_path):
        target = str(tmp_path / "test.md")
        with open(target, "w") as f:
            f.write("old")
        _atomic_rewrite(target, "new")
        tmps = [f for f in os.listdir(tmp_path) if ".tmp." in f]
        assert len(tmps) == 0


# ── Integration: EquilibriumGuard ────────────────────────────────


class TestEquilibriumGuard:
    @pytest.fixture()
    def guard(self):
        return EquilibriumGuard()

    @pytest.fixture()
    def handoff_file(self, tmp_path):
        path = str(tmp_path / "交接_test.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_HANDOFF)
        return path

    def _make_ctx(self, tool_name: str, file_path: str) -> GuardContext:
        return GuardContext(
            tool_name=tool_name,
            tool_input={"file_path": file_path},
            session_id="test",
            cache_dir="",
            hook_event="PostToolUse",
        )

    def test_cleans_on_write(self, guard, handoff_file):
        ctx = self._make_ctx("Write", handoff_file)
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert "cleanup" in result.context.lower() or "清理" in result.context
        assert "2" in result.context

        # Verify file was actually cleaned
        with open(handoff_file, encoding="utf-8") as f:
            content = f.read()
        assert "03-18d" not in content
        assert "03-21a" in content

    def test_cleans_on_edit(self, guard, handoff_file):
        ctx = self._make_ctx("Edit", handoff_file)
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert "2" in result.context

    def test_ignores_non_handoff(self, guard, tmp_path):
        path = str(tmp_path / "readme.md")
        with open(path, "w") as f:
            f.write("hello")
        ctx = self._make_ctx("Write", path)
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_ignores_non_write_tools(self, guard, handoff_file):
        ctx = self._make_ctx("Read", handoff_file)
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_no_op_when_within_limit(self, guard, tmp_path):
        path = str(tmp_path / "交接_small.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_EXACTLY_3)
        ctx = self._make_ctx("Write", path)
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_check_returns_none(self, guard):
        ctx = self._make_ctx("Write", "交接_x.md")
        assert guard.check(ctx) is None

    def test_nonexistent_file(self, guard):
        ctx = self._make_ctx("Write", "/nonexistent/交接_x.md")
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_guard_metadata(self, guard):
        assert guard.name == "equilibrium_guard"
        assert guard.category.name == "QUALITY"

    def test_keep_recent_constant(self):
        assert KEEP_RECENT == 3
