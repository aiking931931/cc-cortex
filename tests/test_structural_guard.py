"""Tests for cc_cortex.structural_guard — rules-based PRM."""

from __future__ import annotations

import textwrap

from cc_cortex.guards.base import GuardAction, GuardContext
from cc_cortex.structural_guard import (
    DEFAULT_THRESHOLDS,
    StructuralGuard,
    StructuralIssue,
    _analyze_js,
    _analyze_python,
    _check_file_length,
    _check_todos,
    check_structural,
    format_feedback,
)

# ── Fixtures ─────────────────────────────────────────────


def _ctx(tool_name="Write", file_path="/tmp/test.py", hook_event="PostToolUse"):
    return GuardContext(
        tool_name=tool_name,
        tool_input={"file_path": file_path},
        session_id="test",
        cache_dir="/tmp/.cc_cortex_cache",
        hook_event=hook_event,
    )


# ── Python: function length ──────────────────────────────


class TestPythonFuncLength:
    def test_short_function_clean(self):
        src = textwrap.dedent("""\
            def foo():
                x = 1
                return x
        """)
        issues = _analyze_python(src, DEFAULT_THRESHOLDS)
        assert not any(i.kind == "func_length" for i in issues)

    def test_long_function_flagged(self):
        lines = ["def big_func():"] + [f"    x_{i} = {i}" for i in range(60)]
        src = "\n".join(lines)
        th = {**DEFAULT_THRESHOLDS, "max_func_lines": 50}
        issues = _analyze_python(src, th)
        funcs = [i for i in issues if i.kind == "func_length"]
        assert len(funcs) == 1
        assert "big_func" in funcs[0].message
        assert "61" in funcs[0].message  # 61 lines

    def test_async_function_length(self):
        lines = ["async def long_async():"] + [f"    await step_{i}()" for i in range(65)]
        src = "\n".join(lines)
        th = {**DEFAULT_THRESHOLDS, "max_func_lines": 50}
        issues = _analyze_python(src, th)
        assert any(i.kind == "func_length" and "long_async" in i.message for i in issues)

    def test_custom_threshold(self):
        lines = ["def medium():"] + [f"    x = {i}" for i in range(30)]
        src = "\n".join(lines)
        # Default (50) should pass
        assert not any(i.kind == "func_length" for i in _analyze_python(src, DEFAULT_THRESHOLDS))
        # Custom (20) should flag
        issues = _analyze_python(src, {"max_func_lines": 20, "max_nesting_depth": 4})
        assert any(i.kind == "func_length" for i in issues)


# ── Python: nesting depth ────────────────────────────────


class TestPythonNesting:
    def test_shallow_nesting_clean(self):
        src = textwrap.dedent("""\
            def foo():
                if True:
                    for x in []:
                        pass
        """)
        issues = _analyze_python(src, DEFAULT_THRESHOLDS)
        assert not any(i.kind == "nesting" for i in issues)

    def test_deep_nesting_flagged(self):
        src = textwrap.dedent("""\
            def deep():
                if True:
                    for x in []:
                        while True:
                            if x:
                                with open("f"):
                                    pass
        """)
        th = {**DEFAULT_THRESHOLDS, "max_nesting_depth": 4}
        issues = _analyze_python(src, th)
        nests = [i for i in issues if i.kind == "nesting"]
        assert len(nests) == 1
        assert "deep" in nests[0].message

    def test_nested_function_not_counted(self):
        src = textwrap.dedent("""\
            def outer():
                if True:
                    def inner():
                        if True:
                            for x in []:
                                while True:
                                    if x:
                                        with open("f"):
                                            pass
        """)
        issues = _analyze_python(src, DEFAULT_THRESHOLDS)
        # outer has depth 1 (just the if), inner has deep nesting
        outer_issues = [i for i in issues if "outer" in i.message and i.kind == "nesting"]
        assert not outer_issues  # outer itself is shallow

    def test_syntax_error_no_crash(self):
        src = "def broken(\n"
        issues = _analyze_python(src, DEFAULT_THRESHOLDS)
        assert issues == []


# ── JS/TS analysis ───────────────────────────────────────


class TestJsAnalysis:
    def test_short_function_clean(self):
        src = "function foo() {\n  return 1;\n}\n"
        issues = _analyze_js(src, DEFAULT_THRESHOLDS)
        assert not any(i.kind == "func_length" for i in issues)

    def test_long_function_flagged(self):
        body = "\n".join(f"  const x{i} = {i};" for i in range(65))
        src = f"function bigFunc() {{\n{body}\n}}\n"
        th = {**DEFAULT_THRESHOLDS, "max_func_lines": 50}
        issues = _analyze_js(src, th)
        funcs = [i for i in issues if i.kind == "func_length"]
        assert len(funcs) == 1
        assert "bigFunc" in funcs[0].message

    def test_arrow_function(self):
        body = "\n".join(f"  const x{i} = {i};" for i in range(65))
        src = f"const handler = async () => {{\n{body}\n}};\n"
        th = {**DEFAULT_THRESHOLDS, "max_func_lines": 50}
        issues = _analyze_js(src, th)
        funcs = [i for i in issues if i.kind == "func_length"]
        assert len(funcs) >= 1

    def test_deep_nesting_flagged(self):
        # 6 levels of indentation (2 spaces each = 12 spaces)
        src = textwrap.dedent("""\
            function deep() {
              if (true) {
                for (let i = 0; i < 10; i++) {
                  while (true) {
                    if (i > 5) {
                      try {
                        console.log(i);
                      } catch(e) {}
                    }
                  }
                }
              }
            }
        """)
        issues = _analyze_js(src, DEFAULT_THRESHOLDS)
        nests = [i for i in issues if i.kind == "nesting"]
        assert len(nests) == 1


# ── TODO/FIXME checks ───────────────────────────────────


class TestTodoCheck:
    def test_no_todos_clean(self):
        src = "def foo():\n    return 1\n"
        assert _check_todos(src, DEFAULT_THRESHOLDS) == []

    def test_few_todos_clean(self):
        src = "# TODO: a\n# FIXME: b\n# HACK: c\n"
        assert _check_todos(src, DEFAULT_THRESHOLDS) == []  # 3 < 5

    def test_many_todos_flagged(self):
        src = "\n".join(f"# TODO: item {i}" for i in range(8))
        issues = _check_todos(src, DEFAULT_THRESHOLDS)
        assert len(issues) == 1
        assert "8" in issues[0].message
        assert "tech debt" in issues[0].message.lower()

    def test_case_insensitive(self):
        src = "\n".join(["# todo: a", "# Todo: b", "# FIXME: c",
                         "# fixme: d", "# HACK: e", "# hack: f"])
        issues = _check_todos(src, DEFAULT_THRESHOLDS)
        assert len(issues) == 1
        assert "6" in issues[0].message

    def test_custom_threshold(self):
        src = "# TODO: a\n# TODO: b\n# TODO: c\n"
        issues = _check_todos(src, {"max_todo_count": 2})
        assert len(issues) == 1


# ── File length check ────────────────────────────────────


class TestFileLength:
    def test_short_file_clean(self):
        src = "x = 1\n" * 100
        assert _check_file_length(src, DEFAULT_THRESHOLDS) == []

    def test_long_file_flagged(self):
        src = "x = 1\n" * 1600
        issues = _check_file_length(src, DEFAULT_THRESHOLDS)
        assert len(issues) == 1
        assert "file_length" == issues[0].kind


# ── check_structural integration ─────────────────────────


class TestCheckStructural:
    def test_clean_python(self, tmp_path):
        f = tmp_path / "clean.py"
        f.write_text("def foo():\n    return 1\n")
        issues = check_structural(str(f))
        assert issues == []

    def test_problematic_python(self, tmp_path):
        lines = ["def monster():"] + [f"    x_{i} = {i}" for i in range(60)]
        lines += [f"# ITEM: fix {i}" for i in range(10)]
        f = tmp_path / "messy.py"
        f.write_text("\n".join(lines))
        issues = check_structural(str(f), thresholds={"max_func_lines": 50})
        kinds = {i.kind for i in issues}
        assert "func_length" in kinds

    def test_unsupported_extension(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        issues = check_structural(str(f))
        assert issues == []

    def test_nonexistent_file(self):
        issues = check_structural("/nonexistent/file.py")
        assert issues == []

    def test_source_param(self):
        src = "\n".join(["def big():"] + [f"    x = {i}" for i in range(60)])
        issues = check_structural(
            "fake.py", source=src, thresholds={"max_func_lines": 50},
        )
        assert any(i.kind == "func_length" for i in issues)

    def test_custom_thresholds(self, tmp_path):
        f = tmp_path / "tight.py"
        lines = ["def func():"] + [f"    x = {i}" for i in range(25)]
        f.write_text("\n".join(lines))
        issues = check_structural(str(f), thresholds={"max_func_lines": 20})
        assert any(i.kind == "func_length" for i in issues)


# ── format_feedback ──────────────────────────────────────


class TestFormatFeedback:
    def test_basic_format(self):
        issues = [
            StructuralIssue("func_length", "`foo` is 80 lines (max 50)", line=10),
            StructuralIssue("todo", "8 TODO/FIXME markers (max 5)"),
        ]
        result = format_feedback("/path/to/file.py", issues)
        assert "📐 Structural" in result
        assert "2 issue(s)" in result
        assert "file.py" in result
        assert "func_length" in result
        assert "L10" in result

    def test_truncation_at_5(self):
        issues = [
            StructuralIssue("func_length", f"func_{i}", line=i)
            for i in range(8)
        ]
        result = format_feedback("big.py", issues)
        assert "... and 3 more" in result

    def test_no_line_number(self):
        issues = [StructuralIssue("todo", "5 TODOs")]
        result = format_feedback("x.py", issues)
        assert "L0" not in result or "[todo]" in result


# ── StructuralGuard (BaseGuard) ──────────────────────────


class TestStructuralGuard:
    def test_ignores_non_write(self):
        guard = StructuralGuard()
        ctx = _ctx(tool_name="Read")
        assert guard.on_post_tool(ctx) is None

    def test_ignores_unsupported_ext(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("hello world\n")
        guard = StructuralGuard()
        ctx = _ctx(file_path=str(f))
        assert guard.on_post_tool(ctx) is None

    def test_clean_file_no_result(self, tmp_path):
        f = tmp_path / "clean.py"
        f.write_text("def foo():\n    return 1\n")
        guard = StructuralGuard()
        ctx = _ctx(file_path=str(f))
        assert guard.on_post_tool(ctx) is None

    def test_problematic_file_returns_allow_context(self, tmp_path):
        lines = ["def monster():"] + [f"    x_{i} = {i}" for i in range(130)]
        f = tmp_path / "big.py"
        f.write_text("\n".join(lines))
        guard = StructuralGuard()
        ctx = _ctx(file_path=str(f))
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW
        assert "📐 Structural" in result.context

    def test_check_returns_none(self):
        """PreToolUse check() always returns None."""
        guard = StructuralGuard()
        ctx = _ctx(hook_event="PreToolUse")
        assert guard.check(ctx) is None

    def test_nonexistent_file(self):
        guard = StructuralGuard()
        ctx = _ctx(file_path="/nonexistent/test.py")
        assert guard.on_post_tool(ctx) is None

    def test_ts_file_analyzed(self, tmp_path):
        body = "\n".join(f"  const x{i} = {i};" for i in range(130))
        f = tmp_path / "big.ts"
        f.write_text(f"function bigFunc() {{\n{body}\n}}\n")
        guard = StructuralGuard()
        ctx = _ctx(file_path=str(f))
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert "bigFunc" in result.context
