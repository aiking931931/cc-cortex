"""concinno.skills.frontmatter_validator — agentskills.io spec alignment.

@module skills.frontmatter_validator
@responsibility Validate ``SKILL.md`` frontmatter against the Anthropic
    Agent Skills open standard (agentskills.io / Apache-2.0, published
    2025-12-18) plus a small Concinno-specific extension namespace
    (``ziq_autotunable`` / ``cosmetic`` / ``concinno_*``) that does not
    collide with any documented spec field. Provides an automatic
    ``--fix`` path that fills in safe defaults for missing optional
    fields while leaving caller-authored values untouched.
@dependencies
    - :mod:`concinno.skill_parser` for permissive YAML-lite frontmatter
      parsing (no PyYAML hard dep).
@exports
    - :data:`SPEC_FIELDS` (frozenset of canonical agentskills.io keys)
    - :data:`CONCINNO_EXTENSION_FIELDS` (Concinno-only namespaced keys)
    - :class:`FrontmatterIssue` (one validation finding)
    - :class:`ValidationReport` (bundle of issues + fix preview)
    - :func:`validate_skill_md` (validate one file)
    - :func:`apply_fix` (rewrite one file with safe defaults filled)
    - :func:`validate_directory` (walk a tree)
    - :func:`format_report_text` (human-readable summary)

Spec coverage (agentskills.io 2025-12-18 snapshot):

    Required:
        - ``name`` (str, snake_case, ≤64 chars)
        - ``description`` (str, ≤1024 chars)

    Recommended (validator nudges if absent):
        - ``version`` (str, semver-ish; default: ``"0.1.0"``)
        - ``triggers`` (list[str]; default: ``[]``)
        - ``user-invocable`` (bool; default: ``true``)

    Optional but documented:
        - ``dependencies`` (list[str] of pip / npm / system requirements)
        - ``platform`` (list[str] subset of {"linux","darwin","win32"})

Concinno extension keys (do not affect spec validation, but cataloged
so the validator can ZIQ-classify them rather than warn ``unknown``):

    - ``ziq_autotunable`` (bool, default ``false``) — whether ZIQ FTRL
      / autotuner is allowed to tweak this skill's runtime params.
    - ``cosmetic`` (bool, default ``false``) — UX/brand-only skill,
      ZIQ should NEVER override user-set values per L0 鐵律 #6.
    - ``concinno_scope`` (str, optional) — one of ``user|public|
      private|project|official`` for routing in Concinno's own scope
      registry; ignored by upstream agentskills.io tools.

Backward-compatibility contract:

    1. Adding a new optional spec field is allowed without bumping
       Concinno major.
    2. Removing or renaming an existing spec field is a major bump.
    3. Tightening a validator (e.g. shortening ``name`` max length) is
       a major bump.
    4. ``apply_fix`` MUST be idempotent: running it twice on the same
       file produces byte-identical output the second time.

Design notes:

    * The validator NEVER raises on a malformed file — it surfaces
      issues as :class:`FrontmatterIssue` records so a caller (CLI, GUI,
      sub-agent) can decide whether to block or warn.
    * ``apply_fix`` only writes when the file actually changes. This
       keeps mtimes stable for users who run the fixer in a tight loop
       (e.g. file watcher).
    * Fix paths only fill ABSENT keys with safe defaults; existing
      caller-authored values (even malformed ones) are NEVER mutated.
       This is per the principle "fix should reduce friction, not steal
       authoring decisions".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from concinno.skill_parser import parse_skill_md

__all__ = [
    "SPEC_FIELDS",
    "REQUIRED_SPEC_FIELDS",
    "RECOMMENDED_SPEC_FIELDS",
    "OPTIONAL_SPEC_FIELDS",
    "CONCINNO_EXTENSION_FIELDS",
    "ALLOWED_PLATFORMS",
    "FrontmatterIssue",
    "Severity",
    "ValidationReport",
    "validate_skill_md",
    "validate_meta",
    "apply_fix",
    "validate_directory",
    "format_report_text",
]


# ── Spec field catalogue ────────────────────────────────────────────

REQUIRED_SPEC_FIELDS: frozenset[str] = frozenset({"name", "description"})

# Fields documented as optional/recommended in agentskills.io. Validator
# emits a ``recommendation`` (not an error) when absent and the
# ``apply_fix`` path fills them with safe defaults.
RECOMMENDED_SPEC_FIELDS: frozenset[str] = frozenset(
    {"version", "triggers", "user-invocable"}
)

OPTIONAL_SPEC_FIELDS: frozenset[str] = frozenset({"dependencies", "platform"})

SPEC_FIELDS: frozenset[str] = (
    REQUIRED_SPEC_FIELDS | RECOMMENDED_SPEC_FIELDS | OPTIONAL_SPEC_FIELDS
)

# Concinno-specific extension keys. Documented separately so the
# validator does not flag them as ``unknown_field`` recommendations.
CONCINNO_EXTENSION_FIELDS: frozenset[str] = frozenset(
    {"ziq_autotunable", "cosmetic", "concinno_scope"}
)

ALLOWED_PLATFORMS: frozenset[str] = frozenset({"linux", "darwin", "win32"})


# Reasonable bounds — chosen to match the public spec wording where
# possible; otherwise picked to mirror the existing concinno corpus
# (sampled 5+ user skills 2026-04-28).
_NAME_MAX_LEN = 64
_DESCRIPTION_MAX_LEN = 1024
_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")
_SEMVER_PATTERN = re.compile(
    r"^\d+(?:\.\d+){0,2}(?:[+-][A-Za-z0-9.]+)?$"
)


# ── Result types ────────────────────────────────────────────────────


class Severity(str, Enum):
    """Severity levels for one finding.

    ``error`` — required field missing / malformed; validator says NO.
    ``warning`` — value present but violates a documented constraint.
    ``recommendation`` — optional field absent or could be tightened;
        ``apply_fix`` will silently fill in a safe default.
    """

    ERROR = "error"
    WARNING = "warning"
    RECOMMENDATION = "recommendation"


@dataclass(frozen=True)
class FrontmatterIssue:
    """One validation finding.

    Attributes:
        field: The frontmatter key in question (e.g. ``"name"``).
            Empty string when the issue applies to the whole file
            (e.g. unparseable frontmatter).
        severity: :class:`Severity`.
        code: Stable machine token (e.g. ``"missing_required"``).
            Callers may switch on this; do not break the values.
        message: Human-readable explanation.
        suggested_value: Value that ``apply_fix`` would write. ``None``
            when the issue cannot be auto-fixed.
    """

    field: str
    severity: Severity
    code: str
    message: str
    suggested_value: Any = None


@dataclass(frozen=True)
class ValidationReport:
    """Per-file validation report.

    Attributes:
        path: Source file path.
        meta: Parsed frontmatter dict (post :func:`parse_skill_md`).
            Empty when frontmatter could not be parsed.
        issues: All findings in declaration order.
        fixable: Subset of ``issues`` that ``apply_fix`` can resolve
            (always severity ``recommendation`` — errors require
            human review).
    """

    path: Path
    meta: dict[str, Any]
    issues: tuple[FrontmatterIssue, ...]
    fixable: tuple[FrontmatterIssue, ...]

    @property
    def has_errors(self) -> bool:
        """True iff any error-level issue exists."""
        return any(i.severity is Severity.ERROR for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        """True iff any warning-level issue exists."""
        return any(i.severity is Severity.WARNING for i in self.issues)


# ── Validators ──────────────────────────────────────────────────────


def _check_name(meta: dict[str, Any]) -> list[FrontmatterIssue]:
    """Validate the ``name`` field. Required; snake/kebab; ≤64 chars."""
    issues: list[FrontmatterIssue] = []
    raw = meta.get("name")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        issues.append(
            FrontmatterIssue(
                field="name",
                severity=Severity.ERROR,
                code="missing_required",
                message=(
                    "name is required (agentskills.io). "
                    "Use snake_case or kebab-case, ≤64 chars."
                ),
            )
        )
        return issues
    if not isinstance(raw, str):
        issues.append(
            FrontmatterIssue(
                field="name",
                severity=Severity.ERROR,
                code="wrong_type",
                message=f"name must be a string, got {type(raw).__name__}.",
            )
        )
        return issues
    if len(raw) > _NAME_MAX_LEN:
        issues.append(
            FrontmatterIssue(
                field="name",
                severity=Severity.WARNING,
                code="too_long",
                message=(
                    f"name is {len(raw)} chars; spec recommends "
                    f"≤{_NAME_MAX_LEN}."
                ),
            )
        )
    if not _NAME_PATTERN.match(raw):
        issues.append(
            FrontmatterIssue(
                field="name",
                severity=Severity.WARNING,
                code="bad_chars",
                message=(
                    "name should match ^[A-Za-z][A-Za-z0-9_-]*$ — "
                    "downstream tools may slugify or reject."
                ),
            )
        )
    return issues


def _check_description(meta: dict[str, Any]) -> list[FrontmatterIssue]:
    """Validate the ``description`` field. Required; ≤1024 chars."""
    issues: list[FrontmatterIssue] = []
    raw = meta.get("description")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        issues.append(
            FrontmatterIssue(
                field="description",
                severity=Severity.ERROR,
                code="missing_required",
                message=(
                    "description is required (agentskills.io). "
                    "Single line summary, ≤1024 chars."
                ),
            )
        )
        return issues
    if not isinstance(raw, str):
        issues.append(
            FrontmatterIssue(
                field="description",
                severity=Severity.ERROR,
                code="wrong_type",
                message=(
                    f"description must be a string, got "
                    f"{type(raw).__name__}."
                ),
            )
        )
        return issues
    if len(raw) > _DESCRIPTION_MAX_LEN:
        issues.append(
            FrontmatterIssue(
                field="description",
                severity=Severity.WARNING,
                code="too_long",
                message=(
                    f"description is {len(raw)} chars; spec recommends "
                    f"≤{_DESCRIPTION_MAX_LEN}."
                ),
            )
        )
    return issues


def _check_version(meta: dict[str, Any]) -> list[FrontmatterIssue]:
    """``version`` is recommended; default ``"0.1.0"``."""
    issues: list[FrontmatterIssue] = []
    if "version" not in meta:
        issues.append(
            FrontmatterIssue(
                field="version",
                severity=Severity.RECOMMENDATION,
                code="missing_recommended",
                message=(
                    "version absent; spec recommends a semver-ish "
                    "string (default '0.1.0')."
                ),
                suggested_value="0.1.0",
            )
        )
        return issues
    raw = meta.get("version")
    if not isinstance(raw, str):
        issues.append(
            FrontmatterIssue(
                field="version",
                severity=Severity.WARNING,
                code="wrong_type",
                message=f"version must be a string, got {type(raw).__name__}.",
            )
        )
        return issues
    if not _SEMVER_PATTERN.match(raw):
        issues.append(
            FrontmatterIssue(
                field="version",
                severity=Severity.WARNING,
                code="bad_format",
                message=(
                    f"version {raw!r} is not semver-ish "
                    "(expected ``MAJOR[.MINOR[.PATCH]][+-PRE]``)."
                ),
            )
        )
    return issues


def _check_triggers(meta: dict[str, Any]) -> list[FrontmatterIssue]:
    """``triggers`` is recommended; default ``[]``. Must be list[str]."""
    issues: list[FrontmatterIssue] = []
    if "triggers" not in meta:
        issues.append(
            FrontmatterIssue(
                field="triggers",
                severity=Severity.RECOMMENDATION,
                code="missing_recommended",
                message=(
                    "triggers absent; spec recommends a list of "
                    "user-facing keywords (default ``[]``)."
                ),
                suggested_value=[],
            )
        )
        return issues
    raw = meta.get("triggers")
    if not isinstance(raw, list):
        issues.append(
            FrontmatterIssue(
                field="triggers",
                severity=Severity.WARNING,
                code="wrong_type",
                message=(
                    f"triggers must be a list of strings, got "
                    f"{type(raw).__name__}."
                ),
            )
        )
        return issues
    for idx, item in enumerate(raw):
        if not isinstance(item, str):
            issues.append(
                FrontmatterIssue(
                    field=f"triggers[{idx}]",
                    severity=Severity.WARNING,
                    code="wrong_item_type",
                    message=(
                        f"triggers[{idx}] must be a string, got "
                        f"{type(item).__name__}."
                    ),
                )
            )
    return issues


def _check_user_invocable(meta: dict[str, Any]) -> list[FrontmatterIssue]:
    """``user-invocable`` recommended; default ``True``. Bool only."""
    issues: list[FrontmatterIssue] = []
    if "user-invocable" not in meta:
        issues.append(
            FrontmatterIssue(
                field="user-invocable",
                severity=Severity.RECOMMENDATION,
                code="missing_recommended",
                message=(
                    "user-invocable absent; spec defaults to ``true`` "
                    "(skill addressable via /<name>)."
                ),
                suggested_value=True,
            )
        )
        return issues
    raw = meta.get("user-invocable")
    if not isinstance(raw, bool):
        issues.append(
            FrontmatterIssue(
                field="user-invocable",
                severity=Severity.WARNING,
                code="wrong_type",
                message=(
                    "user-invocable must be a YAML bool "
                    "(true/false/yes/no)."
                ),
            )
        )
    return issues


def _check_dependencies(meta: dict[str, Any]) -> list[FrontmatterIssue]:
    """Optional ``dependencies`` field; if present must be list[str]."""
    if "dependencies" not in meta:
        return []
    raw = meta.get("dependencies")
    if not isinstance(raw, list):
        return [
            FrontmatterIssue(
                field="dependencies",
                severity=Severity.WARNING,
                code="wrong_type",
                message=(
                    f"dependencies must be a list of strings "
                    f"(pip / npm / system tokens), got "
                    f"{type(raw).__name__}."
                ),
            )
        ]
    issues: list[FrontmatterIssue] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str):
            issues.append(
                FrontmatterIssue(
                    field=f"dependencies[{idx}]",
                    severity=Severity.WARNING,
                    code="wrong_item_type",
                    message=(
                        f"dependencies[{idx}] must be a string, got "
                        f"{type(item).__name__}."
                    ),
                )
            )
    return issues


def _check_platform(meta: dict[str, Any]) -> list[FrontmatterIssue]:
    """Optional ``platform`` list[str] subset of ALLOWED_PLATFORMS."""
    if "platform" not in meta:
        return []
    raw = meta.get("platform")
    if not isinstance(raw, list):
        return [
            FrontmatterIssue(
                field="platform",
                severity=Severity.WARNING,
                code="wrong_type",
                message=(
                    f"platform must be a list of strings "
                    f"(subset of {sorted(ALLOWED_PLATFORMS)}), got "
                    f"{type(raw).__name__}."
                ),
            )
        ]
    issues: list[FrontmatterIssue] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str):
            issues.append(
                FrontmatterIssue(
                    field=f"platform[{idx}]",
                    severity=Severity.WARNING,
                    code="wrong_item_type",
                    message=(
                        f"platform[{idx}] must be a string."
                    ),
                )
            )
            continue
        if item not in ALLOWED_PLATFORMS:
            issues.append(
                FrontmatterIssue(
                    field=f"platform[{idx}]",
                    severity=Severity.WARNING,
                    code="unknown_value",
                    message=(
                        f"platform[{idx}]={item!r} not in "
                        f"{sorted(ALLOWED_PLATFORMS)}."
                    ),
                )
            )
    return issues


def _check_concinno_extensions(meta: dict[str, Any]) -> list[FrontmatterIssue]:
    """Validate Concinno-only ZIQ extension keys (best-effort).

    ``ziq_autotunable`` and ``cosmetic`` MUST be bools when present.
    Absence is silent (these are pure extensions; absence is fine).
    ``concinno_scope`` if present should match the known scope set.
    """
    issues: list[FrontmatterIssue] = []
    for key in ("ziq_autotunable", "cosmetic"):
        if key in meta and not isinstance(meta[key], bool):
            issues.append(
                FrontmatterIssue(
                    field=key,
                    severity=Severity.WARNING,
                    code="wrong_type",
                    message=(
                        f"Concinno extension {key!r} must be a YAML "
                        "bool (true/false)."
                    ),
                )
            )
    if "concinno_scope" in meta:
        raw = meta["concinno_scope"]
        allowed = {"user", "public", "private", "project", "official"}
        if not isinstance(raw, str) or raw.strip().lower() not in allowed:
            issues.append(
                FrontmatterIssue(
                    field="concinno_scope",
                    severity=Severity.WARNING,
                    code="unknown_value",
                    message=(
                        f"concinno_scope={raw!r}; expected one of "
                        f"{sorted(allowed)}."
                    ),
                )
            )
    return issues


_CHECKERS = (
    _check_name,
    _check_description,
    _check_version,
    _check_triggers,
    _check_user_invocable,
    _check_dependencies,
    _check_platform,
    _check_concinno_extensions,
)


def validate_meta(meta: dict[str, Any]) -> tuple[FrontmatterIssue, ...]:
    """Run every checker against an already-parsed frontmatter dict.

    Lower-level entry point — useful for callers that have a parsed
    dict in hand (e.g. the GUI features tab) without a corresponding
    file on disk.
    """
    issues: list[FrontmatterIssue] = []
    for fn in _CHECKERS:
        issues.extend(fn(meta))
    return tuple(issues)


def validate_skill_md(path: Path) -> ValidationReport:
    """Validate one ``SKILL.md`` file against agentskills.io spec.

    Args:
        path: Path to the ``SKILL.md`` to validate. The file is
            permissively parsed via :func:`parse_skill_md` — malformed
            frontmatter surfaces as a single ``ERROR`` issue rather
            than a raised exception.

    Returns:
        :class:`ValidationReport` carrying every finding plus a
        ``fixable`` subset of issues that :func:`apply_fix` will
        resolve when invoked.
    """
    meta = parse_skill_md(path)
    if not meta:
        # parse_skill_md returns ``{}`` for unreadable / no-frontmatter
        # files. We can't validate fields that aren't there; surface
        # one error for the whole file.
        issue = FrontmatterIssue(
            field="",
            severity=Severity.ERROR,
            code="no_frontmatter",
            message=(
                "no parseable frontmatter — expected ``--- ... ---`` "
                "block at the top of the file."
            ),
        )
        return ValidationReport(
            path=path, meta={}, issues=(issue,), fixable=()
        )

    issues = validate_meta(meta)
    fixable = tuple(
        i for i in issues if i.suggested_value is not None
        and i.severity is Severity.RECOMMENDATION
    )
    return ValidationReport(
        path=path, meta=meta, issues=issues, fixable=fixable
    )


# ── Fixer ───────────────────────────────────────────────────────────


def _format_value_for_yaml(value: Any) -> str:
    """Best-effort YAML literal renderer (no PyYAML).

    The set of values produced by :func:`apply_fix` is small and known —
    we only need to handle bool, str, list[str], int. Anything else is
    rendered with ``repr`` and treated as a fallback (the validator
    never suggests a complex value, so this branch is mostly dead but
    safe).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = ", ".join(_format_value_for_yaml(v) for v in value)
        return f"[{items}]"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Quote the value when it contains chars that would be
        # ambiguous in YAML-lite (colons, brackets, leading dashes).
        if any(c in value for c in ":[]{},#&*!|>'\"%@`") or value.strip() != value:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value
    return repr(value)


def apply_fix(path: Path, *, write: bool = True) -> ValidationReport:
    """Fill in safe defaults for absent recommended fields.

    The fixer is intentionally conservative:

    * Touches ONLY absent fields — caller-authored values (even
       malformed ones) are left intact for human review.
    * Inserts new keys at the END of the frontmatter block, before
       the closing ``---`` fence, so the existing key order is
       preserved (downstream diff tools stay quiet).
    * If the file has no frontmatter at all, the fixer does nothing
       and returns the original report — refusing to invent a whole
       block from scratch keeps the tool's mental model simple
       (auto-fix never overwrites human intent).
    * Idempotent: running twice is byte-identical the second time.

    Args:
        path: ``SKILL.md`` to fix.
        write: When False, the fix is computed but not persisted —
            useful for ``--dry-run`` flows / unit tests that want to
            inspect the fixed text.

    Returns:
        Post-fix :class:`ValidationReport`. ``fixable`` is empty
        unless the fix could not be persisted (e.g. unwritable file).
    """
    pre = validate_skill_md(path)
    if not pre.fixable:
        return pre
    if not pre.meta:
        # No frontmatter to extend — see docstring rationale.
        return pre
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return pre
    # Locate the closing fence so we can splice new lines in front of
    # it. The parser already proved the frontmatter is well-formed.
    if not text.startswith("---"):
        return pre
    body = text[3:]
    close_idx = body.find("\n---")
    if close_idx < 0:
        # No closing fence — bail rather than guessing.
        return pre
    head = "---" + body[:close_idx]
    tail = body[close_idx:]  # starts with "\n---"

    additions: list[str] = []
    for issue in pre.fixable:
        # Recheck absence at write time — defensive against caller
        # passing a stale meta dict that no longer reflects the file.
        if issue.field in pre.meta:
            continue
        rendered = _format_value_for_yaml(issue.suggested_value)
        additions.append(f"{issue.field}: {rendered}")
    if not additions:
        return pre

    # Ensure exactly one newline before the inserted lines.
    if not head.endswith("\n"):
        head = head + "\n"
    new_text = head + "\n".join(additions) + tail
    if new_text == text:
        return pre

    if write:
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError:
            return pre

    return validate_skill_md(path)


# ── Directory walker ────────────────────────────────────────────────


def validate_directory(
    root: Path,
    *,
    pattern: str = "**/SKILL.md",
    fix: bool = False,
) -> list[ValidationReport]:
    """Walk ``root`` collecting one report per ``SKILL.md`` found.

    Args:
        root: Directory to walk recursively. Non-existent path returns
            ``[]`` rather than raising.
        pattern: Glob pattern relative to ``root``. Default matches
            every nested ``SKILL.md``.
        fix: When True, runs :func:`apply_fix` on each file before
            reporting. The returned report is the post-fix one so
            CI can use ``not has_errors`` as a green condition.

    Returns:
        List of :class:`ValidationReport`, one per file, in the order
        produced by :func:`pathlib.Path.glob`.
    """
    if not root.is_dir():
        return []
    out: list[ValidationReport] = []
    for path in sorted(root.glob(pattern)):
        if not path.is_file():
            continue
        report = apply_fix(path) if fix else validate_skill_md(path)
        out.append(report)
    return out


# ── Human-readable formatter ────────────────────────────────────────


def format_report_text(reports: Iterable[ValidationReport]) -> str:
    """Format a list of reports for terminal output.

    Mirrors the look of ``ruff check`` — one line per finding, colour
    keys are NOT emitted (callers can pipe through their own colour
    layer; concinno avoids hard-coding ANSI in library code).
    """
    lines: list[str] = []
    total_files = 0
    error_files = 0
    warning_files = 0
    for r in reports:
        total_files += 1
        if r.has_errors:
            error_files += 1
        if r.has_warnings:
            warning_files += 1
        if not r.issues:
            continue
        lines.append(str(r.path))
        for issue in r.issues:
            sev = issue.severity.value.upper().ljust(14)
            field_label = f"[{issue.field}]" if issue.field else "[file]"
            lines.append(f"  {sev} {field_label} {issue.code}: {issue.message}")
    lines.append("")
    lines.append(
        f"Summary: {total_files} file(s), "
        f"{error_files} with errors, {warning_files} with warnings."
    )
    return "\n".join(lines)


