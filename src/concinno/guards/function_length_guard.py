"""concinno.guards.function_length_guard — advise on functions over
100 lines long.

@module function_length_guard
@responsibility On PreToolUse Write / Edit, parse Python files with
    ``ast`` and TS/JS with a regex fallback. Report any function /
    method / arrow function with body length >100 lines. Signal-only
    — many legacy refactors start here.
@dependencies concinno.guards.base (stdlib ast/re only)
@exports FunctionLengthGuard
"""

from __future__ import annotations

import ast
import re

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Configurable threshold (hard-coded until feature_config exposes it).
MAX_FUNCTION_LINES: int = 100

_PY_EXT = (".py",)
_TS_JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Rough JS/TS function detector. Too rough for anything serious, but
# catches the common refactor-worthy case (top-level ``function foo(``
# or ``const foo = (...) => {``). End is either the next top-level
# ``function``/``const`` declaration or EOF — approximate, since
# writing a real JS parser here would bloat the guard.
_JS_FUNCTION_START = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(",
    re.MULTILINE,
)
_JS_ARROW_START = re.compile(
    r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>\s*\{",
    re.MULTILINE,
)


def _find_python_long_funcs(source: str, threshold: int) -> list[tuple[str, int, int]]:
    """Return (name, line, length) for Python functions exceeding *threshold*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    long_funcs: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", None)
            if end is None:
                continue
            length = end - start + 1
            if length > threshold:
                long_funcs.append((node.name, start, length))
    return long_funcs


def _find_js_long_funcs(source: str, threshold: int) -> list[tuple[str, int, int]]:
    """Return (name, line, length) for JS/TS top-level functions exceeding *threshold*.

    Crude: uses brace counting to locate function end. Only scans
    top-level declarations to avoid nested-scope false positives.
    """
    results: list[tuple[str, int, int]] = []
    starts: list[tuple[str, int]] = []
    for m in _JS_FUNCTION_START.finditer(source):
        starts.append((m.group(1), m.start()))
    for m in _JS_ARROW_START.finditer(source):
        starts.append((m.group(1), m.start()))
    starts.sort(key=lambda x: x[1])

    for name, offset in starts:
        # Find opening brace (safe approx — first `{` after the match).
        brace_idx = source.find("{", offset)
        if brace_idx == -1:
            continue
        depth = 0
        end_idx = -1
        for i in range(brace_idx, len(source)):
            ch = source[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        if end_idx == -1:
            continue
        start_line = source.count("\n", 0, offset) + 1
        end_line = source.count("\n", 0, end_idx) + 1
        length = end_line - start_line + 1
        if length > threshold:
            results.append((name, start_line, length))
    return results


class FunctionLengthGuard(BaseGuard):
    """Flag functions exceeding MAX_FUNCTION_LINES.

    Signal-only. Python via AST, TS/JS via regex fallback.
    """

    name = "function_length"
    category = GuardCategory.QUALITY
    feature_name = "function_length"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name not in ("Write", "Edit"):
            return None
        path = ctx.tool_input.get("file_path", "") or ""
        low = path.lower()
        content = (
            ctx.tool_input.get("content", "")
            or ctx.tool_input.get("new_string", "")
            or ""
        )
        if not content:
            return None

        if low.endswith(_PY_EXT):
            long_funcs = _find_python_long_funcs(content, MAX_FUNCTION_LINES)
        elif low.endswith(_TS_JS_EXT):
            long_funcs = _find_js_long_funcs(content, MAX_FUNCTION_LINES)
        else:
            return None

        if not long_funcs:
            return None

        bullets = "\n".join(
            f"  - {name} @ L{line} ({length} lines)"
            for name, line, length in long_funcs[:10]
        )
        overflow = (
            f"\n  ... and {len(long_funcs) - 10} more"
            if len(long_funcs) > 10
            else ""
        )
        msg = (
            f"[function-length] functions exceed {MAX_FUNCTION_LINES} lines "
            f"in `{path}`:\n"
            f"{bullets}{overflow}\n"
            "  Fix: extract helpers / split responsibilities. Signal only — "
            "write proceeds."
        )
        return GuardResult.allow_advisory(context=msg)
