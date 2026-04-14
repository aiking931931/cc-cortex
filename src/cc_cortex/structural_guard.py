"""cc_cortex.structural_guard — Lightweight structural analysis (rules-based PRM).

@module structural_guard
@responsibility PostToolUse AST/regex code quality checks: function length,
    nesting depth, TODO/FIXME debt. Complements ruff/eslint (syntax) with
    structural analysis (maintainability). Zero external dependencies.
@dependencies cc_cortex.core.log, cc_cortex.core.path_utils, cc_cortex.guards.base
@exports check_structural, StructuralGuard
"""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
from dataclasses import dataclass

from cc_cortex.core.log import get_logger
from cc_cortex.core.path_utils import extract_file_path
from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

logger = get_logger(__name__)

# ── Thresholds (overridable via feature_config) ──────────

DEFAULT_THRESHOLDS = {
    "max_func_lines": 120,
    "max_nesting_depth": 5,
    "max_todo_count": 5,
    "max_file_lines": 1500,
}

# Path patterns with relaxed thresholds for standalone scripts/pipelines.
# Uses fnmatch glob syntax against the normalized (forward-slash) path.
PATH_THRESHOLD_OVERRIDES: list[tuple[str, dict]] = [
    ("*/scripts/*", {"max_file_lines": 3000, "max_func_lines": 200}),
    ("*/scripts/**/*", {"max_file_lines": 3000, "max_func_lines": 200}),
    ("*/benchmarks/*", {"max_file_lines": 3000, "max_func_lines": 200}),
    ("benchmarks/*", {"max_file_lines": 3000, "max_func_lines": 200}),
]


def _path_overrides(file_path: str) -> dict:
    """Return threshold overrides if file_path matches any relaxed pattern."""
    normalized = file_path.replace("\\", "/")
    for pattern, overrides in PATH_THRESHOLD_OVERRIDES:
        if fnmatch.fnmatch(normalized, pattern):
            return overrides
    return {}


def _configured_thresholds(file_path: str = "") -> dict:
    """Read thresholds from feature_config + path overrides, falling back to defaults."""
    try:
        from cc_cortex.core.config import get_config
        cfg = get_config()
        base = {
            k: cfg.feature("structural_guard", k) or v
            for k, v in DEFAULT_THRESHOLDS.items()
        }
    except Exception:
        base = dict(DEFAULT_THRESHOLDS)
    if file_path:
        base.update(_path_overrides(file_path))
    return base

# ── Data ─────────────────────────────────────────────────

_TODO_PATTERN = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)

# JS/TS function detection (named functions, arrow functions, methods)
_JS_FUNC_PATTERN = re.compile(
    r"(?:(?:export\s+)?(?:async\s+)?function\s+(\w+)|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(|"
    r"(\w+)\s*\([^)]*\)\s*\{)"
)


@dataclass(frozen=True)
class StructuralIssue:
    """A single structural quality finding."""

    kind: str  # "func_length" | "nesting" | "todo" | "file_length"
    message: str
    line: int = 0


# ── Python AST Analysis ─────────────────────────────────


def _analyze_python(source: str, thresholds: dict) -> list[StructuralIssue]:
    """Analyze Python source via AST."""
    issues: list[StructuralIssue] = []
    max_func = thresholds.get("max_func_lines", 50)
    max_nest = thresholds.get("max_nesting_depth", 4)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        import sys
        print(
            "[cc-cortex] structural_guard: SyntaxError in source, skipping AST analysis",
            file=sys.stderr,
        )
        return issues

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Function length (docstring excluded — comments ≠ logic)
            if node.body:
                start = node.lineno
                end = max(
                    getattr(n, "end_lineno", n.lineno)
                    for n in ast.walk(node)
                    if hasattr(n, "lineno")
                )
                length = end - start + 1

                # Subtract docstring lines if present
                first = node.body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    doc_end = getattr(
                        first, "end_lineno", first.lineno,
                    )
                    docstring_lines = doc_end - first.lineno + 1
                    length -= docstring_lines

                if length > max_func:
                    issues.append(StructuralIssue(
                        kind="func_length",
                        message=f"`{node.name}` is {length} lines (max {max_func})",
                        line=start,
                    ))

            # Nesting depth
            depth = _ast_nesting_depth(node)
            if depth > max_nest:
                issues.append(StructuralIssue(
                    kind="nesting",
                    message=(
                        f"`{node.name}` has nesting depth {depth}"
                        f" (max {max_nest})"
                    ),
                    line=node.lineno,
                ))

    return issues


def _ast_nesting_depth(node: ast.AST, current: int = 0) -> int:
    """Calculate max nesting depth of control flow within a function."""
    nesting_types = (
        ast.If, ast.For, ast.While, ast.With,
        ast.Try, ast.ExceptHandler,
        ast.AsyncFor, ast.AsyncWith,
    )
    max_depth = current
    for child in ast.iter_child_nodes(node):
        if isinstance(child, nesting_types):
            d = _ast_nesting_depth(child, current + 1)
            max_depth = max(max_depth, d)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pass  # Don't descend into nested functions
        else:
            d = _ast_nesting_depth(child, current)
            max_depth = max(max_depth, d)
    return max_depth


# ── JS/TS Regex Analysis ─────────────────────────────────


def _analyze_js(source: str, thresholds: dict) -> list[StructuralIssue]:
    """Analyze JS/TS source via regex (no AST parser needed)."""
    issues: list[StructuralIssue] = []
    lines = source.splitlines()
    max_func = thresholds.get("max_func_lines", 50)
    max_nest = thresholds.get("max_nesting_depth", 4)

    # Function length: find function starts, track braces to find end
    for match in _JS_FUNC_PATTERN.finditer(source):
        name = match.group(1) or match.group(2) or match.group(3) or "anonymous"
        start_offset = match.start()
        start_line = source[:start_offset].count("\n") + 1

        # Find the opening brace
        brace_pos = source.find("{", match.end())
        if brace_pos == -1:
            continue

        # Count braces to find function end
        depth = 1
        pos = brace_pos + 1
        while pos < len(source) and depth > 0:
            ch = source[pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            pos += 1

        end_line = source[:pos].count("\n") + 1
        length = end_line - start_line + 1
        if length > max_func:
            issues.append(StructuralIssue(
                kind="func_length",
                message=f"`{name}` is {length} lines (max {max_func})",
                line=start_line,
            ))

    # Nesting depth: scan indentation-based heuristic
    max_seen = 0
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("//") or stripped.startswith("*"):
            continue
        indent = len(line) - len(stripped)
        # Approximate: 2 or 4 spaces per level
        level = indent // 2
        if level > max_seen:
            max_seen = level

    if max_seen > max_nest:
        issues.append(StructuralIssue(
            kind="nesting",
            message=f"Max nesting ~{max_seen} levels (max {max_nest})",
            line=0,
        ))

    return issues


# ── Common Checks ────────────────────────────────────────


def _check_todos(source: str, thresholds: dict) -> list[StructuralIssue]:
    """Count TODO/FIXME/HACK/XXX markers."""
    matches = _TODO_PATTERN.findall(source)
    count = len(matches)
    limit = thresholds.get("max_todo_count", 5)
    if count > limit:
        return [StructuralIssue(
            kind="todo",
            message=f"{count} TODO/FIXME markers (max {limit}) — tech debt accumulating",
        )]
    return []


def _check_file_length(source: str, thresholds: dict) -> list[StructuralIssue]:
    """Check total file length."""
    line_count = source.count("\n") + 1
    limit = thresholds.get("max_file_lines", 800)
    if line_count > limit:
        return [StructuralIssue(
            kind="file_length",
            message=f"File is {line_count} lines (max {limit}) — consider splitting",
        )]
    return []


# ── Public API ───────────────────────────────────────────

_EXT_ANALYZER = {
    ".py": _analyze_python,
    ".js": _analyze_js,
    ".ts": _analyze_js,
    ".tsx": _analyze_js,
    ".jsx": _analyze_js,
}

SUPPORTED_EXTENSIONS = frozenset(_EXT_ANALYZER.keys())


def check_structural(
    file_path: str,
    *,
    source: str | None = None,
    thresholds: dict | None = None,
) -> list[StructuralIssue]:
    """Run structural analysis on a source file.

    Args:
        file_path: Path to the file (used for extension detection).
        source: File contents. Read from disk if None.
        thresholds: Override DEFAULT_THRESHOLDS.

    Returns:
        List of StructuralIssue findings. Empty = clean.
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    if source is None:
        try:
            source = open(file_path, encoding="utf-8", errors="replace").read()
        except (OSError, IOError):
            return []

    ext = os.path.splitext(file_path)[1].lower()
    analyzer = _EXT_ANALYZER.get(ext)

    issues: list[StructuralIssue] = []

    # Language-specific analysis
    if analyzer:
        issues.extend(analyzer(source, th))

    # Universal checks
    issues.extend(_check_todos(source, th))
    issues.extend(_check_file_length(source, th))

    return issues


def format_feedback(file_path: str, issues: list[StructuralIssue]) -> str:
    """Format issues into additionalContext string."""
    base = os.path.basename(file_path)
    lines = [f"📐 Structural: {len(issues)} issue(s) in {base}"]
    for iss in issues[:5]:
        loc = f"L{iss.line}" if iss.line else ""
        lines.append(f"  • [{iss.kind}] {loc} {iss.message}")
    if len(issues) > 5:
        lines.append(f"  ... and {len(issues) - 5} more")
    return "\n".join(lines)


# ── BaseGuard adapter ────────────────────────────────────


class StructuralGuard(BaseGuard):
    """PostToolUse: lightweight structural analysis (rules-based PRM).

    Checks function length, nesting depth, TODO debt, file length.
    ALLOW + context injection (not DENY) — nudge, don't block.
    Session+file dedup: same file warned once per session (prevents
    attention drain from repeated identical warnings).
    """

    name = "structural_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """No-op PreToolUse — this guard only acts on PostToolUse.

        Returns:
            Always None.
        """
        return None  # PostToolUse only

    def _is_already_warned(self, file_path: str, session_id: str) -> bool:
        """Check if this file was already warned in this session."""
        state_file = self._state_file(session_id)
        if not state_file:
            return False
        try:
            if os.path.isfile(state_file):
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                norm = file_path.lower().replace("\\", "/")
                return norm in state.get("warned_files", [])
        except Exception:
            pass
        return False

    def _mark_warned(self, file_path: str, session_id: str) -> None:
        """Record that this file has been warned in this session."""
        state_file = self._state_file(session_id)
        if not state_file:
            return
        try:
            state: dict = {}
            if os.path.isfile(state_file):
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            norm = file_path.lower().replace("\\", "/")
            warned = state.get("warned_files", [])
            if norm not in warned:
                warned.append(norm)
                # Cap at 200 files to prevent unbounded growth
                if len(warned) > 200:
                    warned = warned[-200:]
            state["warned_files"] = warned
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception:
            pass

    @staticmethod
    def _state_file(session_id: str) -> str:
        """Get state file path for structural dedup."""
        if not session_id:
            return ""
        home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        short_id = session_id[:8]
        return os.path.join(
            home, ".claude", ".cache", "structural_state",
            f"{short_id}.json",
        )

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        """Check function length, nesting depth, TODO count, and file length.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.allow with structural findings as context, or None if clean.
        """
        if ctx.tool_name not in ("Write", "Edit"):
            return None

        file_path = extract_file_path(ctx.tool_input)
        if not file_path or not os.path.isfile(file_path):
            return None

        ext = os.path.splitext(file_path)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return None

        # Session+file dedup: same file → warn only once per session
        session_id = ctx.session_id
        if self._is_already_warned(file_path, session_id):
            return None

        issues = check_structural(file_path, thresholds=_configured_thresholds(file_path))
        if issues:
            self._mark_warned(file_path, session_id)
            return GuardResult.allow(context=format_feedback(file_path, issues))
        return None
