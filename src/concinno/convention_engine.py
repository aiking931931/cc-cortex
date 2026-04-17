"""concinno.convention_engine — Workspace convention enforcement.

@module convention_engine
@responsibility Enforce naming, placement, and template conventions when
    AI creates, moves, or renames files. Industry-aligned defaults,
    fully user-overridable via conventions config.
@dependencies None (stdlib only)
@exports ConventionEngine, check_naming, check_placement, suggest_path

Problem: AI names files randomly every time. "Patent_Draft_v1.md" one day,
"ZIQ-Patent-Claims-Draft-v0.1.md" the next. Folders get polluted.

Solution: Convention-over-configuration engine with sensible defaults.
AI MUST check conventions before creating any file. Users can override
any rule via workspace config.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

# ── Default Conventions ───────────────────────────────────

_DEFAULT_NAMING: dict[str, str] = {
    "patent": "PAT-{seq:03d}_{name}_{variant}.md",
    "handoff": "handoff_{project}.md",
    "handoff_summary": "handoff_{project}_summary.md",
    "handoff_archive": "handoff_{project}_archive.md",
    "feedback": "feedback_{topic}.md",
    "planning": "{project}_{topic}.md",
    "session": "session_{date}_{topic}.md",
    "task_pool": "task-pool.md",
    "memory": "{topic}.md",
}

_DEFAULT_PLACEMENT: list[dict[str, str]] = [
    {"match": r"^PAT-|patent", "dir": "07_Patents/"},
    {"match": r"^handoff_|^交接_", "dir": "06_Handoffs/{project}/"},
    {"match": r"^feedback_", "dir": "memory/"},
    {"match": r"^session_", "dir": "06_Handoffs/{project}/"},
    {"match": r"task-pool", "dir": "06_Handoffs/{project}/"},
    {"match": r"^project_|^user_|^reference_", "dir": "memory/"},
]

_DEFAULT_TEMPLATES: dict[str, dict] = {
    "patent_provisional": {
        "frontmatter": ["title", "inventor", "date", "status"],
        "sections": ["Field", "Background", "Summary", "Claims", "Abstract"],
    },
    "handoff": {
        "frontmatter": ["status", "verified", "last_updated", "tags"],
        "sections": [
            "Status", "Constraints", "Unresolved", "next_step",
            "Recent Sessions", "History", "References",
        ],
    },
    "memory": {
        "frontmatter": ["name", "description", "type"],
    },
    "feedback": {
        "frontmatter": ["name", "description", "type"],
        "body_structure": "rule → Why → How to apply",
    },
}


# ── Data ──────────────────────────────────────────────────

@dataclass
class ConventionResult:
    """Result of a convention check."""

    passed: bool
    suggestion: str = ""  # Suggested fix if not passed
    rule: str = ""        # Which rule triggered


@dataclass
class ConventionConfig:
    """User-overridable conventions. Loaded from workspace config."""

    naming: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_NAMING))
    placement: list[dict[str, str]] = field(
        default_factory=lambda: list(_DEFAULT_PLACEMENT),
    )
    templates: dict[str, dict] = field(
        default_factory=lambda: dict(_DEFAULT_TEMPLATES),
    )


# ── Config Loading ────────────────────────────────────────


def _load_config(workspace: str) -> ConventionConfig:
    """Load conventions from workspace config, with defaults."""
    config = ConventionConfig()
    for candidate in (
        os.path.join(workspace, ".concinno", "conventions.json"),
        os.path.join(workspace, ".claude", "conventions.json"),
    ):
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as f:
                    data = json.load(f)
                if "naming" in data and isinstance(data["naming"], dict):
                    config.naming.update(data["naming"])
                if "placement" in data and isinstance(data["placement"], list):
                    config.placement = data["placement"] + config.placement
                if "templates" in data and isinstance(data["templates"], dict):
                    config.templates.update(data["templates"])
            except (OSError, ValueError):
                pass
            break  # First found wins
    return config


# ── Core Engine ───────────────────────────────────────────


class ConventionEngine:
    """Workspace convention enforcement engine.

    Usage::

        engine = ConventionEngine(workspace="/path/to/project")
        result = engine.check_naming("Patent_Draft_v1.md", file_type="patent")
        if not result.passed:
            print(result.suggestion)  # → "PAT-001_Draft_provisional.md"

        placement = engine.suggest_placement("PAT-001_ZIQ.md")
        # → "07_Patents/PAT-001_ZIQ.md"
    """

    def __init__(self, workspace: str = "") -> None:
        self.workspace = workspace
        self.config = _load_config(workspace) if workspace else ConventionConfig()

    def check_naming(
        self,
        filename: str,
        file_type: Optional[str] = None,
    ) -> ConventionResult:
        """Check if a filename follows conventions.

        Args:
            filename: The filename to check (without directory).
            file_type: Optional type hint (patent/handoff/feedback/etc).

        Returns:
            ConventionResult with passed=True if OK, or suggestion if not.
        """
        detected_type = file_type or self._detect_type(filename)
        if not detected_type:
            return ConventionResult(passed=True)

        pattern = self.config.naming.get(detected_type)
        if not pattern:
            return ConventionResult(passed=True)

        # Check if filename matches the pattern structure
        if self._matches_pattern(filename, pattern):
            return ConventionResult(passed=True)

        return ConventionResult(
            passed=False,
            suggestion=f"Rename to match: {pattern}",
            rule=f"naming.{detected_type}",
        )

    def suggest_placement(
        self,
        filename: str,
        project: str = "",
    ) -> str:
        """Suggest the correct directory for a file.

        Args:
            filename: The filename.
            project: Project name for path interpolation.

        Returns:
            Suggested relative path (dir + filename), or just filename if
            no rule matches.
        """
        for rule in self.config.placement:
            match_pat = rule.get("match", "")
            target_dir = rule.get("dir", "")
            if not match_pat:
                continue
            if re.search(match_pat, filename, re.IGNORECASE):
                resolved_dir = target_dir.replace("{project}", project or "default")
                return os.path.join(resolved_dir, filename)
        return filename

    def check_placement(
        self,
        filepath: str,
        project: str = "",
    ) -> ConventionResult:
        """Check if a file is in the correct directory.

        Args:
            filepath: Relative path from workspace root.
            project: Project name for rule interpolation.

        Returns:
            ConventionResult.
        """
        filename = os.path.basename(filepath)
        suggested = self.suggest_placement(filename, project)
        current_dir = os.path.dirname(filepath)
        suggested_dir = os.path.dirname(suggested)

        if not suggested_dir:
            return ConventionResult(passed=True)

        # Normalize separators for comparison
        current_norm = current_dir.replace("\\", "/").strip("/")
        suggested_norm = suggested_dir.replace("\\", "/").strip("/")

        if current_norm.endswith(suggested_norm):
            return ConventionResult(passed=True)

        return ConventionResult(
            passed=False,
            suggestion=f"Move to: {suggested}",
            rule="placement",
        )

    def get_template(self, file_type: str) -> Optional[dict]:
        """Get the template for a file type.

        Returns:
            Template dict with frontmatter/sections, or None.
        """
        return self.config.templates.get(file_type)

    def check_reuse(
        self,
        function_name: str,
        workspace_root: str = "",
    ) -> ConventionResult:
        """Check if a function/pattern already exists before writing new.

        Simple grep-based check. Returns passed=False if similar name found.
        """
        root = workspace_root or self.workspace
        if not root:
            return ConventionResult(passed=True)

        # Quick search for existing similar functions
        src_dir = os.path.join(root, "src")
        if not os.path.isdir(src_dir):
            return ConventionResult(passed=True)

        for dirpath, _dirs, files in os.walk(src_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    with open(fpath, encoding="utf-8") as f:
                        content = f.read(50000)
                    if f"def {function_name}" in content:
                        rel = os.path.relpath(fpath, root)
                        return ConventionResult(
                            passed=False,
                            suggestion=f"Already exists in {rel}",
                            rule="reuse",
                        )
                except OSError:
                    continue

        return ConventionResult(passed=True)

    # ── Internal ──

    def _detect_type(self, filename: str) -> str:
        """Auto-detect file type from filename patterns."""
        lower = filename.lower()
        if lower.startswith("pat-") or "patent" in lower:
            return "patent"
        if lower.startswith("交接_") or lower.startswith("handoff_"):
            if "_summary" in lower:
                return "handoff_summary"
            if "_archive" in lower:
                return "handoff_archive"
            return "handoff"
        if lower.startswith("feedback_"):
            return "feedback"
        if lower.startswith("session_"):
            return "session"
        if lower == "task-pool.md":
            return "task_pool"
        return ""

    def _matches_pattern(self, filename: str, pattern: str) -> bool:
        """Check if filename structurally matches a naming pattern."""
        # 1. Escape literal dots FIRST (before placeholders introduce regex dots)
        regex = pattern.replace(".", r"\.")
        # 2. Replace placeholders with regex groups
        regex = re.sub(r"\{seq(?::0?\d*d)?\}", r"\\d+", regex)
        regex = re.sub(r"\{[^}]+\}", r".+", regex)
        return bool(re.match(f"^{regex}$", filename))


# ── Module-level convenience ──────────────────────────────

_engine: Optional[ConventionEngine] = None


def get_engine(workspace: str = "") -> ConventionEngine:
    """Get or create the singleton ConventionEngine."""
    global _engine
    if _engine is None or (workspace and _engine.workspace != workspace):
        _engine = ConventionEngine(workspace=workspace)
    return _engine


def check_naming(filename: str, file_type: str = "", workspace: str = "") -> ConventionResult:
    """Convenience: check naming convention."""
    return get_engine(workspace).check_naming(filename, file_type or None)


def suggest_path(filename: str, project: str = "", workspace: str = "") -> str:
    """Convenience: suggest correct file path."""
    return get_engine(workspace).suggest_placement(filename, project)
