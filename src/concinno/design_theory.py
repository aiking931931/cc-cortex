"""concinno.design_theory — Design theory enforcement (Vertical Slice + HITL/AFK + Deep Module).

@module design_theory
@responsibility Enforce software design principles from academic and industry best practices:
    - Vertical Slice discipline (PRD→Issues→TDD tracer bullet)
    - HITL vs AFK classification (human-in-the-loop vs autonomous tasks)
    - Deep Module principle (small interface, large implementation)
    Absorbed from mattpocock/skills design patterns, hardened with Guard Pipeline.
@dependencies concinno.guards.base, concinno.constants
@exports DesignTheoryGuard, check_vertical_slice, classify_hitl_afk, check_deep_module

I build with principles, not just patterns. Theory without practice is empty;
practice without theory is blind.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Vertical Slice ─────────────────────────────────────────

_PLANNING_EXTENSIONS = frozenset({".md", ".txt"})
def _planning_path_markers() -> tuple[str, ...]:
    from concinno.i18n import patterns as i18n_patterns
    return tuple(i18n_patterns("planning_path_markers"))


def _slice_markers_re() -> re.Pattern[str]:
    from concinno.i18n import patterns as i18n_patterns
    parts = i18n_patterns("slice_markers")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else re.compile(r"(?!)")


def _work_item_markers_re() -> re.Pattern[str]:
    from concinno.i18n import patterns as i18n_patterns
    parts = i18n_patterns("work_item_markers")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else re.compile(r"(?!)")


def check_vertical_slice(content: str, file_path: str) -> Optional[str]:
    """Check if a planning file's new work items include end-to-end traceability.

    A proper vertical slice must include at least one testability marker
    (exit criteria, test plan, acceptance criteria, TDD reference).

    Args:
        content: The text content being written.
        file_path: Path to the file being modified.

    Returns:
        Warning message if vertical slice discipline is violated, None if OK.
    """
    if not content or len(content) < 20:
        return None

    # Only check planning files
    ext = os.path.splitext(file_path)[1].lower()
    norm = file_path.replace("\\", "/").lower()

    is_planning = ext in _PLANNING_EXTENSIONS and any(
        m in norm for m in _planning_path_markers()
    )
    if not is_planning:
        return None

    # Must contain new work items
    if not _work_item_markers_re().search(content):
        return None

    # Check for traceability markers
    if _slice_markers_re().search(content):
        return None

    return (
        "Vertical Slice: new work item lacks end-to-end traceability. "
        "Add at least one of: exit criteria, test plan, acceptance criteria, "
        "or TDD reference. Every slice must be verifiable from requirement to test."
    )


# ── HITL vs AFK Classification ─────────────────────────────

def _hitl_re() -> re.Pattern[str]:
    from concinno.i18n import patterns as i18n_patterns
    parts = i18n_patterns("hitl_keywords")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else re.compile(r"(?!)")


def _afk_re() -> re.Pattern[str]:
    from concinno.i18n import patterns as i18n_patterns
    parts = i18n_patterns("afk_keywords")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else re.compile(r"(?!)")


@dataclass(frozen=True)
class TaskClassification:
    """HITL/AFK classification result for a task description."""

    label: str  # "HITL" | "AFK" | "UNKNOWN"
    confidence: float  # 0.0 - 1.0
    reason: str


def classify_hitl_afk(task_description: str) -> TaskClassification:
    """Classify a task as HITL (needs human) or AFK (can run autonomously).

    Args:
        task_description: Task text to classify.

    Returns:
        TaskClassification with label, confidence, and reason.
    """
    if not task_description:
        return TaskClassification("UNKNOWN", 0.0, "Empty task description")

    hitl_hits = len(_hitl_re().findall(task_description))
    afk_hits = len(_afk_re().findall(task_description))

    if hitl_hits > 0 and afk_hits == 0:
        return TaskClassification(
            "HITL", min(0.5 + hitl_hits * 0.2, 1.0),
            f"Contains {hitl_hits} human-required keyword(s)"
        )
    if afk_hits > 0 and hitl_hits == 0:
        return TaskClassification(
            "AFK", min(0.5 + afk_hits * 0.2, 1.0),
            f"Contains {afk_hits} autonomous keyword(s)"
        )
    if hitl_hits > 0 and afk_hits > 0:
        if hitl_hits > afk_hits:
            return TaskClassification(
                "HITL", 0.5,
                f"Mixed signals: {hitl_hits} HITL vs {afk_hits} AFK — defaulting HITL (safer)"
            )
        return TaskClassification(
            "AFK", 0.5,
            f"Mixed signals: {afk_hits} AFK vs {hitl_hits} HITL — slightly autonomous"
        )

    # Heuristic: if task has code-like patterns, likely AFK
    code_pattern = r"\.py|\.ts|\.js|refactor|migrate|lint|test|build"
    code_markers = re.search(code_pattern, task_description, re.IGNORECASE)
    if code_markers:
        return TaskClassification("AFK", 0.3, "Contains code-related patterns — likely automatable")

    return TaskClassification("UNKNOWN", 0.0, "No classification signals found")


def format_hitl_afk_tag(classification: TaskClassification) -> str:
    """Format a HITL/AFK tag for insertion into task lists.

    Args:
        classification: TaskClassification result.

    Returns:
        Formatted tag string like "[HITL]" or "[AFK]".
    """
    if classification.label == "UNKNOWN":
        return ""
    icon = "👤" if classification.label == "HITL" else "🤖"
    return f"[{icon} {classification.label}]"


# ── Deep Module Principle ──────────────────────────────────


@dataclass(frozen=True)
class DeepModuleMetrics:
    """Metrics for evaluating the Deep Module principle."""

    file_path: str
    public_surface: int  # Number of public symbols (exported API)
    implementation_lines: int  # Total lines of implementation
    ratio: float  # implementation / max(public_surface, 1)
    is_shallow: bool  # True if ratio < threshold


def check_deep_module(
    file_path: str,
    *,
    source: str | None = None,
    min_ratio: float = 5.0,
    min_public: int = 3,
) -> Optional[DeepModuleMetrics]:
    """Check if a module follows the Deep Module principle.

    A deep module has a small public interface hiding a large implementation.
    A shallow module exposes too much surface area relative to its implementation.

    Args:
        file_path: Path to the source file.
        source: Source code. Read from disk if None.
        min_ratio: Minimum implementation/public ratio to be considered "deep".
        min_public: Minimum public symbols to trigger the check (very small modules are exempt).

    Returns:
        DeepModuleMetrics if the module is shallow (violates principle), None if deep or exempt.
    """
    if source is None:
        try:
            source = open(file_path, encoding="utf-8", errors="replace").read()
        except (OSError, IOError):
            return None

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".py":
        public_count, impl_lines = _analyze_python_depth(source)
    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        public_count, impl_lines = _analyze_js_depth(source)
    else:
        return None

    if public_count < min_public:
        return None  # Too small to judge

    ratio = impl_lines / max(public_count, 1)
    is_shallow = ratio < min_ratio

    if not is_shallow:
        return None

    return DeepModuleMetrics(
        file_path=file_path,
        public_surface=public_count,
        implementation_lines=impl_lines,
        ratio=round(ratio, 1),
        is_shallow=True,
    )


def _analyze_python_depth(source: str) -> tuple[int, int]:
    """Count public API surface and implementation lines for Python."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, 0

    public_count = 0
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                public_count += 1
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                public_count += 1
                # Count public methods
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not item.name.startswith("_"):
                            public_count += 1
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    # Module-level public variable
                    public_count += 1

    impl_lines = len([
        ln for ln in source.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ])

    return public_count, impl_lines


def _analyze_js_depth(source: str) -> tuple[int, int]:
    """Count public API surface and implementation lines for JS/TS."""
    # Count exports as public surface
    export_pattern = re.compile(
        r"export\s+(?:default\s+)?(?:function|class|const|let|type|interface|enum)\s+(\w+)",
    )
    exports = export_pattern.findall(source)
    public_count = len(exports)

    # Also count named exports at bottom
    barrel_pattern = re.compile(r"export\s*\{([^}]+)\}")
    for match in barrel_pattern.finditer(source):
        names = [n.strip().split(" as ")[0].strip() for n in match.group(1).split(",")]
        public_count += len([n for n in names if n])

    impl_lines = len([
        ln for ln in source.splitlines()
        if ln.strip() and not ln.strip().startswith("//")
    ])

    return public_count, impl_lines


# ── Sub-agent Parallel Design ──────────────────────────────

PARALLEL_DESIGN_PROMPT_TEMPLATE = (
    "You are design variant {variant_id}. Your constraint: {constraint}. "
    "Given the requirements below, produce an independent implementation plan. "
    "Do NOT coordinate with other variants — divergence is the point.\n\n"
    "Requirements:\n{requirements}\n\n"
    "Output: implementation plan with file list, API surface, and trade-offs."
)

# Predefined constraint sets for common scenarios
DESIGN_CONSTRAINTS = {
    "performance_vs_readability": [
        "Optimize for minimal latency and memory. Sacrifice readability if needed.",
        "Optimize for readability and maintainability. Performance is secondary.",
        "Balance both. Use standard patterns and document performance-critical paths.",
    ],
    "monolith_vs_modular": [
        "Single file, minimal abstractions. Inline everything.",
        "Maximum modularity. Each concern in its own module.",
        "Pragmatic split: 2-3 files max, grouped by lifecycle.",
    ],
    "defensive_vs_minimal": [
        "Maximum safety: validate every input, handle every edge case.",
        "Minimal: trust internal callers, validate only at boundaries.",
        "Tiered: strict at public API, relaxed internally.",
    ],
}


def generate_parallel_prompts(
    requirements: str,
    constraint_set: str = "performance_vs_readability",
    custom_constraints: list[str] | None = None,
) -> list[str]:
    """Generate prompts for parallel sub-agent design exploration.

    Each prompt tasks an agent with a different constraint, producing
    independent design alternatives for human/LLM comparison.

    Args:
        requirements: The feature requirements text.
        constraint_set: Key from DESIGN_CONSTRAINTS, or "custom".
        custom_constraints: Custom constraint list (overrides constraint_set).

    Returns:
        List of prompts, one per design variant.
    """
    constraints = custom_constraints or DESIGN_CONSTRAINTS.get(
        constraint_set, DESIGN_CONSTRAINTS["performance_vs_readability"]
    )

    return [
        PARALLEL_DESIGN_PROMPT_TEMPLATE.format(
            variant_id=chr(65 + i),  # A, B, C...
            constraint=constraint,
            requirements=requirements,
        )
        for i, constraint in enumerate(constraints)
    ]


# ── Guard Integration ──────────────────────────────────────


class DesignTheoryGuard(BaseGuard):
    """Enforce design theory principles on planning and code files.

    PreToolUse: Check vertical slice discipline on planning file edits.
    PostToolUse: Check deep module principle on code file writes.

    I don't just check syntax — I check whether the design serves the whole.
    """

    name = "design_theory"
    category = GuardCategory.QUALITY
    step_back_reason = "design principle violation detected"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """PreToolUse: enforce vertical slice on planning files.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny if planning file lacks traceability, None otherwise.
        """
        if ctx.tool_name not in WRITE_TOOLS_EXT:
            return None

        path = ctx.tool_input.get("file_path", "") or ctx.tool_input.get("path", "")
        content = ""
        if ctx.tool_name == "Write":
            content = ctx.tool_input.get("content", "")
        elif ctx.tool_name == "Edit":
            content = ctx.tool_input.get("new_string", "")
        elif ctx.tool_name == "NotebookEdit":
            content = ctx.tool_input.get("new_source", "")

        reason = check_vertical_slice(content, path)
        if reason:
            return GuardResult.deny(
                reason,
                context=(
                    "Vertical Slice discipline requires every new work item to be "
                    "traceable from requirement to verification. Add exit criteria "
                    "or a test plan reference."
                ),
            )
        return None

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        """PostToolUse: check deep module principle on written code files.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.allow with context if module is shallow, None otherwise.
        """
        if ctx.tool_name not in ("Write", "Edit"):
            return None

        path = ctx.tool_input.get("file_path", "") or ctx.tool_input.get("path", "")
        if not path or not os.path.isfile(path):
            return None

        ext = os.path.splitext(path)[1].lower()
        if ext not in (".py", ".ts", ".tsx", ".js", ".jsx"):
            return None

        metrics = check_deep_module(path)
        if metrics:
            return GuardResult.allow(
                context=(
                    f"Deep Module: {os.path.basename(path)} has "
                    f"{metrics.public_surface} public symbols but only "
                    f"{metrics.implementation_lines} impl lines "
                    f"(ratio {metrics.ratio}, min {5.0}). "
                    f"Consider: fewer exports, or more implementation behind the interface."
                ),
            )
        return None
