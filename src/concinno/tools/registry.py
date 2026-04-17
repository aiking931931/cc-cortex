"""concinno.tools.registry — deferred tool registry with lazy-load search.

@module tools.registry
@responsibility Split tool surface into "core" (always in prompt) and
    "deferred" (loaded on demand via :meth:`ToolRegistry.search`). Mirrors
    CC's ``tools/ToolSearchTool/ToolSearchTool.ts`` so a CCC agent can
    scale past the ~40-tool prompt budget without paying ~6k tokens per
    long session.
@dependencies concinno.tool_executor.Tool (Protocol, runtime_checkable)
@exports ToolEntry, ToolSearchResult, ToolRegistry, get_default_registry,
    format_function_block

Design mirrors CC's split: core tools are cheap to describe and called
every turn (Read, Grep, Glob). Deferred tools are rare-use or large (Shell,
MCP wrappers) — the LLM pays a `ToolSearch` round-trip only when it
actually needs them. Lazy-import means the Python class is never even
loaded until the first ``get(name)`` call — a session that never touches
Shell never imports ``subprocess``.

Query semantics intentionally match CC:

- ``select:A,B,C`` → exact multi-select; missing names silently skipped
  (same as CC — selecting an already-loaded core tool is a harmless
  no-op).
- keyword query → tokenise + score over parsed tool-name parts and
  descriptions using CC's weights (10 / 5 / 3 / 2). ``+prefix`` marks a
  required term.

Caching: a private ``_search_impl`` wrapped by ``functools.lru_cache``
keys on ``(query, max_results, version)`` where ``version`` bumps on
every mutation of the deferred dict — matching CC's
``maybeInvalidateCache`` pattern.
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from ..tool_executor import Tool

logger = logging.getLogger("concinno.tools.registry")

# Matches CC's truncation budget for tool descriptions in the search index.
_MAX_DESCRIPTION_CHARS = 250

# Word-boundary regex used for description / hint scoring — mirrors CC's
# ``compileTermPatterns`` which builds ``\b<term>\b`` per query term.
_WORD_BOUNDARY = r"\b{}\b"


# ── Dataclasses ────────────────────────────────────────────────────────


@dataclass
class ToolEntry:
    """One deferred tool entry: import target + cached resolved instance.

    ``import_path`` uses the ``module:attr`` form familiar from
    entry-point specs, e.g. ``"concinno.tools.builtin.shell:Shell"``.
    ``resolved`` caches the instantiated tool after the first
    :meth:`ToolRegistry.get` hit so subsequent lookups are O(1).
    """

    name: str
    import_path: str
    description: str
    max_results_default: int = 5
    resolved: Tool | None = None


@dataclass
class ToolSearchResult:
    """One hit from :meth:`ToolRegistry.search`.

    ``source`` disambiguates core vs deferred for callers that want to
    render the results differently (CC uses this to decide whether to
    inject a schema block or just remind the LLM the tool is already
    loaded).
    """

    name: str
    description: str
    score: float
    source: Literal["core", "deferred"]


# ── Helpers ────────────────────────────────────────────────────────────


def _parse_tool_name(name: str) -> tuple[list[str], str, bool]:
    """Split a tool name into searchable parts.

    Mirrors CC's ``parseToolName`` (ToolSearchTool.ts:132). MCP tools
    (``mcp__server__action``) split on ``__`` then ``_``; regular tools
    split on CamelCase boundaries and underscores.

    Returns ``(parts, full, is_mcp)``.
    """
    if name.startswith("mcp__"):
        without_prefix = name[len("mcp__") :].lower()
        raw_parts = without_prefix.split("__")
        parts = [p for chunk in raw_parts for p in chunk.split("_") if p]
        full = without_prefix.replace("__", " ").replace("_", " ")
        return parts, full, True

    # Regular tool: break CamelCase into spaces, then split.
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ").lower()
    parts = [p for p in spaced.split() if p]
    return parts, " ".join(parts), False


def _score_tool(
    name: str,
    description: str,
    terms: list[str],
    *,
    required: list[str] | None = None,
) -> float:
    """Score a tool against search terms using CC's weights.

    CC weights (ToolSearchTool.ts:266-290):
        exact part match   → 10 (MCP: 12)
        partial part match →  5 (MCP: 6)
        full-name fallback →  3 (only if no other hit yet)
        description match  →  2 (word-boundary)

    If any required (``+term``) term fails to match anywhere, returns 0.
    """
    parts, full, is_mcp = _parse_tool_name(name)
    desc_lower = description.lower()
    required = required or []

    # Required-term gate: every +term must land somewhere.
    for req in required:
        pattern = re.compile(_WORD_BOUNDARY.format(re.escape(req)))
        hit = (
            req in parts
            or any(req in p for p in parts)
            or bool(pattern.search(desc_lower))
        )
        if not hit:
            return 0.0

    score = 0.0
    for term in terms:
        pattern = re.compile(_WORD_BOUNDARY.format(re.escape(term)))

        # Exact part match first — highest signal.
        if term in parts:
            score += 12 if is_mcp else 10
        elif any(term in p for p in parts):
            score += 6 if is_mcp else 5

        # Full-name fallback: only when nothing else has fired yet.
        if term in full and score == 0:
            score += 3

        # Description word-boundary match.
        if pattern.search(desc_lower):
            score += 2

    return score


def _load_tool(import_path: str) -> Tool | None:
    """Resolve ``module:attr`` to an instantiated tool.

    Returns ``None`` (and logs) if import fails for any reason. Never
    raises — callers treat the unknown-tool case uniformly.
    """
    if ":" not in import_path:
        logger.error("registry: bad import_path %r (missing ':')", import_path)
        return None
    module_name, attr = import_path.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        logger.error("registry: failed to import %s: %s", module_name, exc)
        return None
    cls = getattr(module, attr, None)
    if cls is None:
        logger.error("registry: %s has no attribute %r", module_name, attr)
        return None
    try:
        instance = cls() if callable(cls) else cls
    except Exception as exc:  # noqa: BLE001 — tool construction may fail
        logger.error("registry: failed to instantiate %s: %s", import_path, exc)
        return None
    if not isinstance(instance, Tool):
        logger.error("registry: %s does not satisfy Tool protocol", import_path)
        return None
    return instance


# ── Registry ───────────────────────────────────────────────────────────


class ToolRegistry:
    """Core + deferred tool registry with lazy-loading keyword search.

    Usage::

        reg = ToolRegistry()
        reg.register_core(FileRead())
        reg.register_deferred(
            name="Shell",
            import_path="concinno.tools.builtin.shell:Shell",
            description="Run bash commands with guard + timeout.",
        )
        results = reg.search("select:Shell")
        shell_tool = reg.get("Shell")  # lazy-imports on first call
    """

    def __init__(self) -> None:
        self._core: dict[str, Tool] = {}
        self._deferred: dict[str, ToolEntry] = {}
        # Incremented on every mutation so lru_cache keys stay valid.
        self._version: int = 0

    # ── Mutation ───────────────────────────────────────────────────

    def register_core(self, tool: Tool) -> None:
        """Add ``tool`` to the core set (always in prompt)."""
        if tool.name in self._deferred:
            msg = f"name collision: {tool.name!r} already registered as deferred"
            raise ValueError(msg)
        if tool.name in self._core:
            msg = f"core tool {tool.name!r} already registered"
            raise ValueError(msg)
        self._core[tool.name] = tool
        self._bump_version()

    def register_deferred(
        self,
        name: str,
        import_path: str,
        description: str,
        *,
        max_results_default: int = 5,
    ) -> None:
        """Add a deferred entry. The class is not imported until needed."""
        if name in self._core:
            msg = f"name collision: {name!r} already registered as core"
            raise ValueError(msg)
        if name in self._deferred:
            msg = f"deferred tool {name!r} already registered"
            raise ValueError(msg)
        if len(description) > _MAX_DESCRIPTION_CHARS:
            logger.warning(
                "registry: description for %r is %d chars (>%d); truncated in scoring",
                name,
                len(description),
                _MAX_DESCRIPTION_CHARS,
            )
        self._deferred[name] = ToolEntry(
            name=name,
            import_path=import_path,
            description=description,
            max_results_default=max_results_default,
        )
        self._bump_version()

    def _bump_version(self) -> None:
        """Invalidate the search lru_cache by bumping the version counter."""
        self._version += 1

    # ── Accessors ──────────────────────────────────────────────────

    def get(self, name: str) -> Tool | None:
        """Return a Tool by name. Lazy-imports + caches deferred entries.

        Returns ``None`` if the name is unknown or if a deferred import
        fails (error is logged, not raised — mirrors CC's soft-fail).
        """
        if name in self._core:
            return self._core[name]
        entry = self._deferred.get(name)
        if entry is None:
            return None
        if entry.resolved is not None:
            return entry.resolved
        tool = _load_tool(entry.import_path)
        if tool is None:
            return None
        entry.resolved = tool
        return tool

    def list_core(self) -> list[str]:
        """Names of all core tools, in registration order."""
        return list(self._core.keys())

    def list_deferred(self) -> list[str]:
        """Names of all deferred tools, in registration order."""
        return list(self._deferred.keys())

    def list_all(self) -> list[str]:
        """Union of core + deferred names."""
        return [*self._core.keys(), *self._deferred.keys()]

    # ── Search ─────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> list[ToolSearchResult]:
        """Search tools. Two modes:

        - ``select:A,B,C`` → exact multi-select. Missing names skipped.
        - keyword → tokenise + score deferred tools. Exact-name match
          across *all* tools short-circuits (matches CC fast path).

        Results sort by score (desc), ties break by registration order.
        """
        return list(self._search_impl(query, max_results, self._version))

    @lru_cache(maxsize=128)  # noqa: B019 — intentional per-instance cache
    def _search_impl(
        self,
        query: str,
        max_results: int,
        version: int,  # noqa: ARG002 — part of cache key, bumps invalidate
    ) -> tuple[ToolSearchResult, ...]:
        query = query.strip()
        if not query:
            return ()

        # Exact select: mode.
        select_match = re.match(r"^select:(.+)$", query, re.IGNORECASE)
        if select_match:
            requested = [
                s.strip() for s in select_match.group(1).split(",") if s.strip()
            ]
            results: list[ToolSearchResult] = []
            for req in requested:
                if req in self._core:
                    results.append(
                        ToolSearchResult(
                            name=req,
                            description=self._core[req].description,
                            score=1.0,
                            source="core",
                        )
                    )
                elif req in self._deferred:
                    entry = self._deferred[req]
                    results.append(
                        ToolSearchResult(
                            name=entry.name,
                            description=entry.description,
                            score=1.0,
                            source="deferred",
                        )
                    )
                # Missing names silently dropped — matches CC behavior.
            return tuple(results[:max_results])

        # Keyword mode.
        query_lower = query.lower()

        # Fast path: exact name hit across both buckets.
        for name, tool in self._core.items():
            if name.lower() == query_lower:
                return (
                    ToolSearchResult(
                        name=name,
                        description=tool.description,
                        score=100.0,
                        source="core",
                    ),
                )
        for name, entry in self._deferred.items():
            if name.lower() == query_lower:
                return (
                    ToolSearchResult(
                        name=name,
                        description=entry.description,
                        score=100.0,
                        source="deferred",
                    ),
                )

        # Partition +required and optional terms.
        raw_terms = [t for t in query_lower.split() if t]
        required: list[str] = []
        optional: list[str] = []
        for term in raw_terms:
            if term.startswith("+") and len(term) > 1:
                required.append(term[1:])
            else:
                optional.append(term)
        all_terms = [*required, *optional] if required else raw_terms
        if not all_terms:
            return ()

        # Score deferred tools only (core tools are already in prompt).
        scored: list[tuple[float, int, ToolSearchResult]] = []
        for idx, entry in enumerate(self._deferred.values()):
            score = _score_tool(
                entry.name,
                entry.description[:_MAX_DESCRIPTION_CHARS],
                all_terms,
                required=required,
            )
            if score > 0:
                scored.append(
                    (
                        score,
                        idx,
                        ToolSearchResult(
                            name=entry.name,
                            description=entry.description,
                            score=score,
                            source="deferred",
                        ),
                    )
                )

        # Sort by score desc, then registration order (idx) asc.
        scored.sort(key=lambda x: (-x[0], x[1]))
        return tuple(r for _, _, r in scored[:max_results])


# ── Formatting ─────────────────────────────────────────────────────────


def format_function_block(results: list[ToolSearchResult]) -> str:
    """Emit a ``<function>{...}</function>`` block for prompt injection.

    Matches CC's post-search injection shape: one ``<function>`` line per
    result wrapped in a ``<functions>...</functions>`` container. The
    embedded JSON is a minimal stub — callers that need full JSONSchema
    should fetch ``registry.get(name).input_schema`` after selection.

    Returns an empty string when ``results`` is empty.
    """
    if not results:
        return ""
    import json as _json

    lines = ["<functions>"]
    for r in results:
        payload = {
            "description": r.description,
            "name": r.name,
        }
        lines.append(f"<function>{_json.dumps(payload, ensure_ascii=False)}</function>")
    lines.append("</functions>")
    return "\n".join(lines)


# ── Default factory ────────────────────────────────────────────────────


def get_default_registry() -> ToolRegistry:
    """Build CCC's default registry: 5 core (file I/O + search) + Shell deferred.

    Rationale: Read/Glob/Grep dominate most sessions; Write/Edit are frequent
    enough that deferring them costs more in round-trips than it saves in
    prompt tokens. Shell is comparatively rare and has a heavy import
    footprint (subprocess, guards) — deferring it is a clean win.
    """
    # Late import to avoid cycles and to mirror user patterns.
    from .builtin import FileEdit, FileGlob, FileGrep, FileRead, FileWrite

    reg = ToolRegistry()
    reg.register_core(FileRead())
    reg.register_core(FileWrite())
    reg.register_core(FileEdit())
    reg.register_core(FileGlob())
    reg.register_core(FileGrep())
    reg.register_deferred(
        name="Shell",
        import_path="concinno.tools.builtin.shell:Shell",
        description=(
            "Execute bash commands with destruction_guard + bash_validators "
            "+ 2-stage classifier + auto-background after 15s. Use for "
            "builds, tests, git, and shell pipelines."
        ),
    )
    return reg


__all__ = [
    "ToolEntry",
    "ToolRegistry",
    "ToolSearchResult",
    "format_function_block",
    "get_default_registry",
]
