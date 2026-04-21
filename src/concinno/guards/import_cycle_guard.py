"""concinno.guards.import_cycle_guard — detect import cycles inside
the directory that owns the file being edited.

@module import_cycle_guard
@responsibility On PreToolUse Write / Edit of a Python file, parse
    the new content's top-level ``import`` / ``from ... import``
    statements and combine with the sibling ``.py`` files in the same
    directory to build a lightweight import graph. Flag any cycle
    introduced.
@dependencies concinno.guards.base (stdlib ast/os/re only)
@exports ImportCycleGuard, build_import_graph, detect_cycle
"""

from __future__ import annotations

import ast
import os

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Safety cap — large directories scan too many siblings and risk
# blowing past the tool-call latency budget. Same directory with
# >80 .py files skips the deep scan and issues no warning.
_MAX_SIBLING_COUNT = 80
_MAX_FILE_BYTES = 200_000


def _top_level_imports(source: str) -> list[str]:
    """Return imported module names (top-level + from X import) from *source*.

    Uses ``ast`` so we skip strings and comments cleanly. Returns
    module names only (not the ``Y`` in ``from X import Y``) so we can
    build a module-level graph.
    """
    if not source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Relative imports have module=None or module starts without dots.
            if node.level > 0 and node.module:
                names.append(node.module.split(".")[0])
            elif node.module:
                names.append(node.module.split(".")[0])
    return names


def _module_name(file_path: str) -> str:
    """Module name = basename without .py extension."""
    base = os.path.basename(file_path)
    if base.endswith(".py"):
        return base[:-3]
    return base


def build_import_graph(
    directory: str,
    *,
    override_file: str = "",
    override_source: str = "",
) -> dict[str, set[str]]:
    """Build a local import graph for siblings in *directory*.

    When *override_file* matches a sibling, its imports are taken from
    *override_source* instead of disk — this models the pending Write/Edit.

    Returns a mapping of ``module_name -> {imported_sibling_module, ...}``.
    Imports that don't resolve to a sibling file are ignored — we only
    track intra-directory edges (enough to catch the common cycle
    without walking the whole project).
    """
    if not directory or not os.path.isdir(directory):
        return {}
    siblings: list[str] = []
    try:
        entries = os.listdir(directory)
    except OSError:
        return {}
    for name in entries:
        if not name.endswith(".py"):
            continue
        if name == "__init__.py":
            continue
        siblings.append(name)
    if len(siblings) > _MAX_SIBLING_COUNT:
        return {}

    sibling_modules = {_module_name(n) for n in siblings}
    override_mod = _module_name(override_file) if override_file else ""

    graph: dict[str, set[str]] = {}
    for name in siblings:
        full = os.path.join(directory, name)
        mod = _module_name(name)
        if mod == override_mod:
            source = override_source
        else:
            try:
                if os.path.getsize(full) > _MAX_FILE_BYTES:
                    source = ""
                else:
                    with open(full, encoding="utf-8", errors="ignore") as f:
                        source = f.read()
            except OSError:
                source = ""
        imports = _top_level_imports(source)
        graph[mod] = {i for i in imports if i in sibling_modules and i != mod}

    # Ensure override_mod is in graph even if it doesn't exist on disk yet.
    if override_mod and override_mod not in graph:
        imports = _top_level_imports(override_source)
        graph[override_mod] = {
            i for i in imports if i in sibling_modules and i != override_mod
        }
    return graph


def detect_cycle(graph: dict[str, set[str]], start: str) -> list[str] | None:
    """DFS from *start* searching for a cycle reaching back to *start*.

    Returns the cycle path (e.g. ``["A", "B", "A"]``) or None.
    Deterministic: sibling neighbours iterated in sorted order so the
    reported path is stable across runs.
    """
    if start not in graph:
        return None

    path: list[str] = []
    visited: set[str] = set()

    def dfs(node: str) -> list[str] | None:
        if node in path:
            # Found a cycle — slice path from first occurrence.
            idx = path.index(node)
            return [*path[idx:], node]
        if node in visited:
            return None
        path.append(node)
        for nxt in sorted(graph.get(node, set())):
            result = dfs(nxt)
            if result is not None:
                return result
        path.pop()
        visited.add(node)
        return None

    return dfs(start)


class ImportCycleGuard(BaseGuard):
    """Warn when a Write/Edit introduces an import cycle in same-dir siblings.

    Signal-only. Lightweight — only same-directory scan, bounded by
    _MAX_SIBLING_COUNT.
    """

    name = "import_cycle"
    category = GuardCategory.QUALITY
    feature_name = "import_cycle"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name not in ("Write", "Edit"):
            return None
        path = ctx.tool_input.get("file_path", "") or ""
        if not path.endswith(".py"):
            return None
        content = (
            ctx.tool_input.get("content", "")
            or ctx.tool_input.get("new_string", "")
            or ""
        )
        if not content:
            return None
        directory = os.path.dirname(path)
        if not directory:
            return None

        graph = build_import_graph(
            directory, override_file=path, override_source=content,
        )
        if not graph:
            return None
        start = _module_name(path)
        cycle = detect_cycle(graph, start)
        if not cycle or len(cycle) < 2:
            return None

        cycle_str = " -> ".join(cycle)
        msg = (
            f"[import-cycle] new imports in `{os.path.basename(path)}` introduce "
            f"a cycle within `{directory}`:\n"
            f"  {cycle_str}\n"
            "  Fix: break the cycle by extracting the shared symbols into a "
            "new module, or use a local (function-scope) import as an escape. "
            "Signal only — write proceeds."
        )
        return GuardResult.allow_advisory(context=msg)
