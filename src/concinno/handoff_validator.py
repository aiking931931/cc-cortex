"""concinno.handoff_validator — Handoff file schema validation.

@module handoff_validator
@responsibility Validate handoff markdown files (frontmatter, structural rules,
               pending-task priorities, API/deploy reference links).
@dependencies concinno.guards.base
@exports ValidationResult, validate_file, validate_dir,
         format_report, check_handoff_on_write, HandoffGuard
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Configuration ───────────────────────────────────────────────

REQUIRED_FIELDS: dict[str, dict[str, Any]] = {
    "status": {"valid": ["active", "paused", "archived"], "default": "active"},
    "verified": {"valid": ["true", "false"], "default": "false"},
    "last_updated": {"pattern": r"^\d{4}-\d{2}-\d{2}$", "default": ""},
}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Hard-layer patterns
PRIORITY_SECTION_RE = re.compile(r"(priority|P0|P1|P2)", re.IGNORECASE)
API_KEYWORDS_RE = re.compile(r"(API|deploy|VPS|key|port)", re.IGNORECASE)
def _api_ref_re() -> re.Pattern[str]:
    """Build API reference regex from i18n patterns."""
    from concinno.i18n import patterns as i18n_patterns
    parts = i18n_patterns("handoff_file_ref")
    if not parts:
        parts = ["quick.?find", "index", "kb/", "kb_"]
    return re.compile("(" + "|".join(parts) + ")", re.IGNORECASE)
SUB_HANDOFF_RE = re.compile(r"_P\d+-\d+\.md$")


# ── Data structures ─────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Result of validating a single handoff file."""

    path: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fixed: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.warnings


# ── Parsing ─────────────────────────────────────────────────────


def parse_frontmatter(content: str) -> tuple[dict[str, str] | None, str]:
    """Parse YAML frontmatter. Returns (fields_dict, rest_of_content)."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return None, content
    fields: dict[str, str] = {}
    for line in m.group(1).strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            fields[key.strip()] = val.strip()
    rest = content[m.end():]
    return fields, rest


# ── Validation ──────────────────────────────────────────────────


def validate_file(
    filepath: str,
    fix: bool = False,
    required_fields: dict[str, dict[str, Any]] | None = None,
) -> ValidationResult:
    """Validate a single handoff file.

    Args:
        filepath: Path to the handoff markdown file.
        fix: If True, auto-add missing frontmatter with defaults.
        required_fields: Override default required fields.
    """
    result = ValidationResult(path=filepath)
    req = required_fields or REQUIRED_FIELDS

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    fields, rest = parse_frontmatter(content)

    # Set dynamic defaults
    for key, spec in req.items():
        if key == "last_updated" and not spec.get("default"):
            spec["default"] = str(date.today())

    if fields is None:
        result.errors.append("MISSING frontmatter")
        if fix:
            fm_lines = ["---"]
            for key, spec in req.items():
                fm_lines.append(f"{key}: {spec['default']}")
            fm_lines.append("---")
            new_content = "\n".join(fm_lines) + "\n" + content
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(new_content)
            result.fixed = True
        return result

    needs_fix = False
    for key, spec in req.items():
        if key not in fields:
            result.errors.append(f"MISSING field: {key}")
            if fix:
                fields[key] = spec["default"]
                needs_fix = True
        elif "valid" in spec and fields[key] not in spec["valid"]:
            result.errors.append(f"INVALID {key}={fields[key]} (valid: {spec['valid']})")
        elif "pattern" in spec and not re.match(spec["pattern"], fields[key]):
            result.errors.append(f"INVALID {key}={fields[key]} (expected: {spec['pattern']})")

    if fix and needs_fix:
        fm_lines = ["---"]
        for key in req:
            fm_lines.append(f"{key}: {fields.get(key, req[key]['default'])}")
        fm_lines.append("---")
        new_content = "\n".join(fm_lines) + "\n" + rest
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_content)
        result.fixed = True

    # Hard-layer checks
    is_sub = bool(SUB_HANDOFF_RE.search(filepath))

    if "⬜" in content and not bool(PRIORITY_SECTION_RE.search(content)) and not is_sub:
        result.warnings.append(
            "HARD[dispatch] Has pending tasks (⬜) but no priority section (add P0/P1/P2)"
        )

    has_api = bool(API_KEYWORDS_RE.search(content))
    has_ref = bool(_api_ref_re().search(content))
    if has_api and not has_ref and not is_sub:
        result.warnings.append(
            "HARD[api_ref] Contains API/deploy content but no reference link"
        )

    return result


def validate_dir(
    handoff_dir: str,
    pattern: str = "",
    fix: bool = False,
    required_fields: dict[str, dict[str, Any]] | None = None,
) -> list[ValidationResult]:
    """Validate all handoff files in a directory.

    Args:
        handoff_dir: Directory containing handoff subdirectories.
        pattern: Glob pattern relative to handoff_dir.
        fix: If True, auto-fix missing frontmatter.
        required_fields: Override default required fields.

    Returns:
        List of ValidationResult objects.
    """
    if not pattern:
        # Build pattern from all handoff prefixes
        prefixes = _handoff_patterns()
        globs = [f"*/{p}*.md" for p in prefixes]
        files: list[str] = []
        for g in globs:
            files.extend(glob.glob(os.path.join(handoff_dir, g)))
        files = sorted(set(files))
    else:
        files = sorted(glob.glob(os.path.join(handoff_dir, pattern)))
    results: list[ValidationResult] = []
    for f in files:
        results.append(validate_file(f, fix=fix, required_fields=required_fields))
    return results


# ── CLI formatting ──────────────────────────────────────────────


def format_report(results: list[ValidationResult]) -> str:
    """Format validation results as human-readable text."""
    lines: list[str] = []
    total_errors = 0
    total_warnings = 0

    for r in results:
        name = os.path.basename(r.path)
        if r.ok:
            lines.append(f"[OK] {name}")
        else:
            for e in r.errors:
                lines.append(f"[ERR] {name}: {e}")
                total_errors += 1
            for w in r.warnings:
                lines.append(f"[HARD] {name}: {w}")
                total_warnings += 1

    lines.append("")
    lines.append(f"Files: {len(results)} | Errors: {total_errors} | Warnings: {total_warnings}")
    return "\n".join(lines)


# ── PostToolUse Integration ────────────────────────────────────


def _handoff_patterns() -> tuple[str, ...]:
    """Load handoff file prefixes from i18n."""
    from concinno.i18n import patterns as i18n_patterns
    prefixes = i18n_patterns("handoff_prefixes")
    return tuple(prefixes) if prefixes else ("handoff_",)


def check_handoff_on_write(
    tool_name: str,
    tool_input: dict,
) -> str | None:
    """Check handoff file structure on Write/Edit. Returns error msg or None.

    Returns lint-level error string if handoff file has structural issues,
    or None if valid / not a handoff file. Performance: <3ms.
    """
    if tool_name not in ("Write", "Edit", "NotebookEdit"):
        return None

    file_path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or ""
    )
    if not file_path:
        return None

    basename = os.path.basename(file_path)
    if not any(basename.startswith(p) for p in _handoff_patterns()):
        return None

    # Skip non-markdown files (e.g. handoff_engine.py, handoff_validator.py)
    if not basename.endswith(".md"):
        return None

    if not os.path.isfile(file_path):
        return None

    try:
        result = validate_file(file_path, fix=False)
        issues = []
        for err in result.errors:
            issues.append(f"❌ {err}")
        for warn in result.warnings:
            issues.append(f"⚠ {warn}")

        if not issues:
            return None

        return (
            f"Handoff Format: {basename} has {len(issues)} issue(s):\n"
            + "\n".join(issues)
        )
    except Exception:
        return None


# ── BaseGuard adapter ───────────────────────────────────────────


class HandoffGuard(BaseGuard):
    """PostToolUse: validate handoff file format on Write."""

    name = "handoff_guard"
    feature_name = "handoff_format"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """No-op PreToolUse — this guard only acts on PostToolUse.

        Returns:
            Always None.
        """
        return None  # PostToolUse only

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        """Validate handoff file format + auto-classify HITL/AFK tasks after Write.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.allow with format issues or HITL/AFK tags as context.
        """
        if ctx.tool_name != "Write":
            return None

        parts: list[str] = []

        # 1. Handoff format validation
        feedback = check_handoff_on_write(ctx.tool_name, ctx.tool_input)
        if feedback:
            parts.append(feedback)

        # 2. HITL/AFK auto-classification for task items
        content = ctx.tool_input.get("content", "")
        file_path = ctx.tool_input.get("file_path", "")
        if content and ("⬜" in content or "⏸" in content):
            norm = (file_path or "").replace("\\", "/").lower()
            is_handoff = any(p.rstrip("_") in norm for p in _handoff_patterns())
            if is_handoff:
                try:
                    import re

                    from concinno.design_theory import (
                        classify_hitl_afk,
                        format_hitl_afk_tag,
                    )
                    tasks = re.findall(r"[⬜⏸]\s*(.+)", content)
                    tagged = []
                    for task in tasks[:5]:
                        cls = classify_hitl_afk(task)
                        tag = format_hitl_afk_tag(cls)
                        if tag:
                            tagged.append(f"  {tag} {task[:50]}")
                    if tagged:
                        parts.append(
                            "HITL/AFK classification:\n"
                            + "\n".join(tagged)
                        )
                except Exception:
                    pass

        if parts:
            return GuardResult.allow(context="\n".join(parts))
        return None
