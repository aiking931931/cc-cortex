"""Tests for concinno.tools.registry — deferred tool registry + search.

Matches CC's ToolSearchTool semantics: select: exact mode, keyword
scoring with weighted part/description matches, lazy import with
per-instance caching, and bounded lru_cache invalidation.
"""

from __future__ import annotations

import logging
import sys

import pytest

from concinno.tools.registry import (
    ToolEntry,
    ToolRegistry,
    ToolSearchResult,
    _parse_tool_name,
    _score_tool,
    format_function_block,
    get_default_registry,
)

# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeTool:
    """Minimal Tool-protocol-compliant stub."""

    is_concurrency_safe = True

    def __init__(self, name: str = "Fake", description: str = "a fake tool") -> None:
        self.name = name
        self.description = description

    def call(self, **kwargs: object) -> str:  # noqa: ARG002
        return "ok"


class _FakeWrite(_FakeTool):
    is_concurrency_safe = False


# Module-level fakes usable from import_path strings.
_test_fake_tool = _FakeTool(name="Lazy", description="lazy-loaded fake")


def _make_lazy() -> _FakeTool:
    return _FakeTool(name="LazyFactory", description="factory made")


class _BadProtocol:
    """Lacks name/description/call — shouldn't satisfy Tool."""


# ── register_core / register_deferred ─────────────────────────────────


class TestRegistration:
    def test_register_core_adds_to_core(self):
        reg = ToolRegistry()
        tool = _FakeTool("A", "first")
        reg.register_core(tool)
        assert reg.list_core() == ["A"]
        assert reg.list_deferred() == []

    def test_register_deferred_adds_to_deferred(self):
        reg = ToolRegistry()
        reg.register_deferred("Lazy", "tests.test_tools_registry:_test_fake_tool", "desc")
        assert reg.list_deferred() == ["Lazy"]
        assert reg.list_core() == []

    def test_list_all_is_union(self):
        reg = ToolRegistry()
        reg.register_core(_FakeTool("A", "a"))
        reg.register_deferred("B", "tests.test_tools_registry:_test_fake_tool", "b")
        assert set(reg.list_all()) == {"A", "B"}
        assert reg.list_all() == ["A", "B"]

    def test_duplicate_core_name_raises(self):
        reg = ToolRegistry()
        reg.register_core(_FakeTool("A", "a"))
        with pytest.raises(ValueError, match="already registered"):
            reg.register_core(_FakeTool("A", "b"))

    def test_duplicate_deferred_name_raises(self):
        reg = ToolRegistry()
        reg.register_deferred("X", "mod:attr", "desc")
        with pytest.raises(ValueError, match="already registered"):
            reg.register_deferred("X", "mod:attr2", "desc2")

    def test_cross_namespace_collision_core_then_deferred(self):
        reg = ToolRegistry()
        reg.register_core(_FakeTool("Same", "core"))
        with pytest.raises(ValueError, match="name collision"):
            reg.register_deferred("Same", "mod:attr", "deferred")

    def test_cross_namespace_collision_deferred_then_core(self):
        reg = ToolRegistry()
        reg.register_deferred("Same", "mod:attr", "deferred")
        with pytest.raises(ValueError, match="name collision"):
            reg.register_core(_FakeTool("Same", "core"))


# ── get() ─────────────────────────────────────────────────────────────


class TestGet:
    def test_get_core_returns_instance(self):
        reg = ToolRegistry()
        tool = _FakeTool("A", "a")
        reg.register_core(tool)
        assert reg.get("A") is tool

    def test_get_deferred_lazy_imports(self):
        reg = ToolRegistry()
        reg.register_deferred(
            "Lazy",
            "tests.test_tools_registry:_test_fake_tool",
            "desc",
        )
        tool = reg.get("Lazy")
        assert tool is _test_fake_tool

    def test_get_deferred_caches_resolved_instance(self):
        reg = ToolRegistry()
        reg.register_deferred(
            "Lazy",
            "tests.test_tools_registry:_make_lazy",
            "desc",
        )
        first = reg.get("Lazy")
        second = reg.get("Lazy")
        assert first is second  # cached — factory not re-called

    def test_get_unknown_returns_none(self):
        reg = ToolRegistry()
        assert reg.get("Nope") is None

    def test_get_bad_import_path_returns_none(self, caplog):
        reg = ToolRegistry()
        reg.register_deferred("X", "nonexistent_module_xyz:Attr", "desc")
        with caplog.at_level(logging.ERROR, logger="concinno.tools.registry"):
            result = reg.get("X")
        assert result is None
        assert any("failed to import" in rec.message for rec in caplog.records)

    def test_get_missing_attr_returns_none(self, caplog):
        reg = ToolRegistry()
        reg.register_deferred("X", "tests.test_tools_registry:NoSuchAttr", "desc")
        with caplog.at_level(logging.ERROR, logger="concinno.tools.registry"):
            result = reg.get("X")
        assert result is None

    def test_get_non_tool_returns_none(self, caplog):
        reg = ToolRegistry()
        reg.register_deferred("X", "tests.test_tools_registry:_BadProtocol", "desc")
        with caplog.at_level(logging.ERROR, logger="concinno.tools.registry"):
            result = reg.get("X")
        assert result is None

    def test_get_malformed_import_path_returns_none(self, caplog):
        reg = ToolRegistry()
        reg.register_deferred("X", "no_colon_path", "desc")
        with caplog.at_level(logging.ERROR, logger="concinno.tools.registry"):
            result = reg.get("X")
        assert result is None


# ── search: select: mode ──────────────────────────────────────────────


class TestSearchSelect:
    def test_select_single_deferred(self):
        reg = ToolRegistry()
        reg.register_deferred("Shell", "mod:attr", "bash runner")
        results = reg.search("select:Shell")
        assert len(results) == 1
        assert results[0].name == "Shell"
        assert results[0].source == "deferred"

    def test_select_multi_comma_separated(self):
        reg = ToolRegistry()
        reg.register_core(_FakeTool("Read", "reader"))
        reg.register_core(_FakeTool("Write", "writer"))
        results = reg.search("select:Read,Write")
        names = [r.name for r in results]
        assert names == ["Read", "Write"]
        assert all(r.source == "core" for r in results)

    def test_select_mixed_core_and_deferred(self):
        reg = ToolRegistry()
        reg.register_core(_FakeTool("Read", "reader"))
        reg.register_deferred("Shell", "mod:attr", "bash")
        results = reg.search("select:Read,Shell")
        names = [r.name for r in results]
        sources = [r.source for r in results]
        assert names == ["Read", "Shell"]
        assert sources == ["core", "deferred"]

    def test_select_missing_names_silently_dropped(self):
        reg = ToolRegistry()
        reg.register_deferred("Shell", "mod:attr", "bash")
        results = reg.search("select:Shell,Nonexistent")
        assert len(results) == 1
        assert results[0].name == "Shell"

    def test_select_all_missing_returns_empty(self):
        reg = ToolRegistry()
        results = reg.search("select:A,B,C")
        assert results == []

    def test_select_trims_whitespace(self):
        reg = ToolRegistry()
        reg.register_deferred("Shell", "mod:attr", "bash")
        results = reg.search("select: Shell ")
        assert len(results) == 1
        assert results[0].name == "Shell"


# ── search: keyword mode ──────────────────────────────────────────────


class TestSearchKeyword:
    def _populate(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register_deferred(
            "Shell",
            "mod:attr",
            "Execute bash commands with destruction guard and timeout.",
        )
        reg.register_deferred(
            "NotebookEdit",
            "mod:attr",
            "Edit a Jupyter notebook cell in place.",
        )
        reg.register_deferred(
            "WebFetch",
            "mod:attr",
            "Fetch a URL and return the rendered markdown.",
        )
        reg.register_deferred(
            "mcp__github__issues",
            "mod:attr",
            "List and create GitHub issues.",
        )
        return reg

    def test_keyword_finds_shell_via_description(self):
        reg = self._populate()
        results = reg.search("bash")
        names = [r.name for r in results]
        assert "Shell" in names
        assert results[0].name == "Shell"

    def test_keyword_finds_notebook_via_name_part(self):
        reg = self._populate()
        results = reg.search("notebook")
        assert results[0].name == "NotebookEdit"

    def test_keyword_finds_mcp_by_server_name(self):
        reg = self._populate()
        results = reg.search("github")
        assert results[0].name == "mcp__github__issues"

    def test_keyword_no_match_returns_empty(self):
        reg = self._populate()
        assert reg.search("quantum unicorn") == []

    def test_keyword_respects_max_results(self):
        reg = ToolRegistry()
        for i in range(10):
            reg.register_deferred(f"Tool{i}", "mod:attr", "shell bash execute")
        results = reg.search("shell", max_results=3)
        assert len(results) == 3

    def test_keyword_exact_name_fast_path(self):
        reg = self._populate()
        results = reg.search("Shell")
        assert len(results) == 1
        assert results[0].name == "Shell"
        assert results[0].score == 100.0  # fast-path sentinel

    def test_keyword_empty_query_returns_empty(self):
        reg = self._populate()
        assert reg.search("") == []
        assert reg.search("   ") == []

    def test_keyword_required_term_filters(self):
        reg = self._populate()
        # +notebook forces notebook match; "edit" is optional
        results = reg.search("+notebook edit")
        assert len(results) == 1
        assert results[0].name == "NotebookEdit"

    def test_keyword_required_term_with_no_match(self):
        reg = self._populate()
        results = reg.search("+nonexistent shell")
        assert results == []

    def test_keyword_scores_sorted_descending(self):
        reg = self._populate()
        # "bash" hits Shell description with word boundary
        results = reg.search("bash")
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)


# ── lru_cache invalidation ────────────────────────────────────────────


class TestCacheInvalidation:
    def test_repeat_search_is_cached(self):
        reg = ToolRegistry()
        reg.register_deferred("Shell", "mod:attr", "bash execute")
        reg._search_impl.cache_clear()
        reg.search("bash")
        info1 = reg._search_impl.cache_info()
        reg.search("bash")
        info2 = reg._search_impl.cache_info()
        assert info2.hits == info1.hits + 1

    def test_register_invalidates_cache(self):
        reg = ToolRegistry()
        reg.register_deferred("Shell", "mod:attr", "bash")
        results1 = reg.search("bash")
        assert len(results1) == 1

        # Add another matching tool — cache should reflect the new state.
        reg.register_deferred("BashRunner", "mod:attr", "run bash scripts")
        results2 = reg.search("bash")
        names2 = {r.name for r in results2}
        assert names2 == {"Shell", "BashRunner"}


# ── description budget warning ────────────────────────────────────────


class TestDescriptionBudget:
    def test_over_budget_description_emits_warning(self, caplog):
        reg = ToolRegistry()
        long_desc = "x" * 300
        with caplog.at_level(logging.WARNING, logger="concinno.tools.registry"):
            reg.register_deferred("Big", "mod:attr", long_desc)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("250" in w.message or ">" in w.message for w in warnings)
        # Still registered — not rejected.
        assert "Big" in reg.list_deferred()

    def test_under_budget_no_warning(self, caplog):
        reg = ToolRegistry()
        with caplog.at_level(logging.WARNING, logger="concinno.tools.registry"):
            reg.register_deferred("Small", "mod:attr", "short")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert not warnings


# ── format_function_block ─────────────────────────────────────────────


class TestFormatFunctionBlock:
    def test_empty_results_empty_string(self):
        assert format_function_block([]) == ""

    def test_single_result_produces_block(self):
        results = [
            ToolSearchResult(
                name="Shell",
                description="run bash",
                score=10.0,
                source="deferred",
            )
        ]
        block = format_function_block(results)
        assert block.startswith("<functions>")
        assert block.endswith("</functions>")
        assert "<function>" in block
        assert '"name": "Shell"' in block
        assert '"description": "run bash"' in block

    def test_multiple_results_one_function_each(self):
        results = [
            ToolSearchResult("A", "alpha", 5, "deferred"),
            ToolSearchResult("B", "beta", 3, "deferred"),
        ]
        block = format_function_block(results)
        assert block.count("<function>") == 2
        assert '"name": "A"' in block
        assert '"name": "B"' in block


# ── default registry ──────────────────────────────────────────────────


class TestDefaultRegistry:
    def test_default_has_5_core_and_6_deferred(self):
        # 5 core = Read/Write/Edit/Glob/Grep (always in prompt).
        # 6 deferred = Shell + 5 optional built-in tools
        # (PdfRead/PdfExtract/HtmlToText/DuckDbQuery/RssFetch, lazy-loaded
        # behind optional extras pdf/html/data/rss).
        reg = get_default_registry()
        assert len(reg.list_core()) == 5
        assert len(reg.list_deferred()) == 6

    def test_default_deferred_includes_shell_and_optionals(self):
        reg = get_default_registry()
        deferred = set(reg.list_deferred())
        assert deferred == {
            "Shell",
            "PdfRead",
            "PdfExtract",
            "HtmlToText",
            "DuckDbQuery",
            "RssFetch",
        }

    def test_default_core_includes_file_tools(self):
        reg = get_default_registry()
        core = set(reg.list_core())
        # Built-in tool names from concinno.tools.builtin — Read/Write/Edit/Glob/Grep
        assert "Read" in core
        assert "Write" in core
        assert "Edit" in core
        assert "Glob" in core
        assert "Grep" in core

    def test_default_shell_lazy_loads(self):
        reg = get_default_registry()
        # Before get(): shell entry not yet resolved.
        entry = reg._deferred["Shell"]
        assert entry.resolved is None
        shell = reg.get("Shell")
        assert shell is not None
        # Shell tool's canonical name inside concinno.tools.builtin is "Bash"
        # (matches CC's BashTool). The registry key is "Shell" — that's the
        # search handle; the underlying tool.name is orthogonal.
        assert shell.name in ("Shell", "Bash")
        # After get(): resolved cached.
        assert reg._deferred["Shell"].resolved is shell

    def test_default_search_bash_finds_shell(self):
        reg = get_default_registry()
        results = reg.search("bash")
        assert any(r.name == "Shell" for r in results)


# ── pure helpers ──────────────────────────────────────────────────────


class TestParseToolName:
    def test_camel_case_split(self):
        parts, full, is_mcp = _parse_tool_name("NotebookEdit")
        assert parts == ["notebook", "edit"]
        assert full == "notebook edit"
        assert is_mcp is False

    def test_mcp_tool_split(self):
        parts, full, is_mcp = _parse_tool_name("mcp__github__issues")
        assert "github" in parts
        assert "issues" in parts
        assert is_mcp is True

    def test_underscore_split(self):
        parts, _full, _is_mcp = _parse_tool_name("file_read")
        assert parts == ["file", "read"]


class TestScoreTool:
    def test_exact_part_match_scores_10(self):
        score = _score_tool("Shell", "", ["shell"])
        assert score == 10

    def test_mcp_exact_part_match_scores_12(self):
        score = _score_tool("mcp__github__issues", "", ["github"])
        assert score == 12

    def test_description_match_scores_2(self):
        score = _score_tool("Unrelated", "runs bash commands", ["bash"])
        assert score == 2

    def test_no_match_scores_0(self):
        assert _score_tool("Shell", "bash runner", ["nonexistent"]) == 0

    def test_required_term_missing_returns_0(self):
        score = _score_tool(
            "Shell", "runs bash", ["shell"], required=["nonexistent"]
        )
        assert score == 0


# ── ToolEntry dataclass ───────────────────────────────────────────────


class TestToolEntry:
    def test_default_resolved_is_none(self):
        entry = ToolEntry(
            name="X",
            import_path="m:a",
            description="d",
        )
        assert entry.resolved is None
        assert entry.max_results_default == 5


# Ensure tests module is discoverable for lazy-import paths.
sys.modules.setdefault("tests.test_tools_registry", sys.modules[__name__])
