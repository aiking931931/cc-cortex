"""concinno.ssot_guard — Single Source of Truth enforcement (PostToolUse gate).

@module ssot_guard
@responsibility PostToolUse regex scan: detects local duplications of centralized
    definitions (color maps, persona lists, hardcoded strings) and DENY the edit.
    Rules loaded from project-level `.ssot-rules.json` — configurable, cross-device.
@dependencies concinno.core.log, concinno.core.path_utils, concinno.guards.base
@exports SSOTGuard

Design pattern: Design Token SSOT enforcement (Airbnb/Shopify style).
Each project defines its own rules in `.ssot-rules.json` at repo root.
No rules file = guard is silent. Rules are regex patterns with fix instructions.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass

from concinno.core.log import get_logger
from concinno.core.path_utils import extract_file_path
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

logger = get_logger(__name__)

# Cache: workspace → (mtime, rules)
_rules_cache: dict[str, tuple[float, list[SSOTRule]]] = {}


@dataclass(frozen=True)
class SSOTRule:
    """A single SSOT enforcement rule.

    Attributes:
        id: Unique rule identifier (e.g. "no-local-colormap").
        pattern: Regex pattern to detect violations.
        message: Human-readable fix instruction shown on deny.
        scope: Glob pattern for files to scan (e.g. "*.tsx").
        exclude: Glob patterns for files to skip (e.g. ["**/avatars.ts"]).
        severity: "deny" (block edit) or "context" (inject feedback only).
    """

    id: str
    pattern: str
    message: str
    scope: str = "*.tsx"
    exclude: tuple[str, ...] = ()
    severity: str = "deny"  # "deny" | "context"


def _load_rules(workspace: str) -> list[SSOTRule]:
    """Load SSOT rules from .ssot-rules.json in workspace root.

    Returns cached rules if file hasn't changed. Returns [] if no file.
    """
    if not workspace:
        return []

    rules_path = os.path.join(workspace, ".ssot-rules.json")
    if not os.path.isfile(rules_path):
        return []

    try:
        mtime = os.path.getmtime(rules_path)
    except OSError:
        return []

    # Check cache
    cached = _rules_cache.get(workspace)
    if cached and cached[0] == mtime:
        return cached[1]

    # Parse rules
    try:
        with open(rules_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("ssot_guard: failed to load %s: %s", rules_path, e)
        return []

    rules: list[SSOTRule] = []
    for entry in data.get("rules", []):
        if not isinstance(entry, dict):
            continue
        rule_id = entry.get("id", "")
        pattern = entry.get("pattern", "")
        message = entry.get("message", "")
        if not (rule_id and pattern and message):
            continue

        exclude_raw = entry.get("exclude", [])
        exclude = tuple(exclude_raw) if isinstance(exclude_raw, list) else ()

        rules.append(SSOTRule(
            id=rule_id,
            pattern=pattern,
            message=message,
            scope=entry.get("scope", "*.tsx"),
            exclude=exclude,
            severity=entry.get("severity", "deny"),
        ))

    _rules_cache[workspace] = (mtime, rules)
    logger.debug("ssot_guard: loaded %d rules from %s", len(rules), rules_path)
    return rules


def _file_matches_scope(file_path: str, scope: str, exclude: tuple[str, ...]) -> bool:
    """Check if file matches scope glob and is not excluded."""
    basename = os.path.basename(file_path)
    # Normalize to forward slashes for glob matching
    rel_path = file_path.replace("\\", "/")

    # Check scope (match basename or full path)
    if not (fnmatch.fnmatch(basename, scope) or fnmatch.fnmatch(rel_path, scope)):
        return False

    # Check excludes
    for ex in exclude:
        if fnmatch.fnmatch(basename, ex) or fnmatch.fnmatch(rel_path, ex):
            return False

    return True


@dataclass(frozen=True)
class SSOTViolation:
    """A detected SSOT violation."""

    rule_id: str
    message: str
    line: int
    severity: str


def check_ssot(file_path: str, source: str, rules: list[SSOTRule]) -> list[SSOTViolation]:
    """Scan source for SSOT violations against the given rules.

    Returns list of violations found.
    """
    violations: list[SSOTViolation] = []

    for rule in rules:
        if not _file_matches_scope(file_path, rule.scope, rule.exclude):
            continue

        try:
            compiled = re.compile(rule.pattern, re.MULTILINE)
        except re.error as e:
            logger.warning("ssot_guard: invalid regex in rule %s: %s", rule.id, e)
            continue

        for match in compiled.finditer(source):
            line = source[:match.start()].count("\n") + 1
            violations.append(SSOTViolation(
                rule_id=rule.id,
                message=rule.message,
                line=line,
                severity=rule.severity,
            ))

    return violations


def format_deny(file_path: str, violations: list[SSOTViolation]) -> str:
    """Format violations into a deny reason string."""
    base = os.path.basename(file_path)
    lines = [f"🔒 SSOT violation in {base} — use centralized definitions:"]
    for v in violations[:5]:
        loc = f"L{v.line}" if v.line else ""
        lines.append(f"  ✗ [{v.rule_id}] {loc} {v.message}")
    if len(violations) > 5:
        lines.append(f"  ... and {len(violations) - 5} more")
    return "\n".join(lines)


class SSOTGuard(BaseGuard):
    """PostToolUse: SSOT enforcement via project-level .ssot-rules.json.

    Gate DENY on violations (no soft warnings — per soft-warning negative ROI law).
    Rules are project-configurable, cached by mtime, cross-device via git.

    Design: Airbnb/Shopify Design Token Pattern enforcement.
    Each project defines forbidden patterns + fix instructions.
    No .ssot-rules.json = guard is silent (zero overhead).
    """

    name = "ssot_guard"
    category = GuardCategory.QUALITY
    step_back_reason = "SSOT violation — use centralized definition instead of local duplicate"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """No-op PreToolUse — this guard only acts on PostToolUse."""
        return None

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        """Scan edited/written files for SSOT violations.

        Returns:
            GuardResult.deny on violations, None if clean.
        """
        if ctx.tool_name not in ("Write", "Edit"):
            return None

        file_path = extract_file_path(ctx.tool_input)
        if not file_path or not os.path.isfile(file_path):
            return None

        # Only check relevant extensions
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in (".tsx", ".ts", ".jsx", ".js", ".py"):
            return None

        rules = _load_rules(ctx.workspace)
        if not rules:
            return None

        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            return None

        violations = check_ssot(file_path, source, rules)
        if not violations:
            return None

        # Separate deny vs context-only violations
        denies = [v for v in violations if v.severity == "deny"]
        infos = [v for v in violations if v.severity == "context"]

        if denies:
            return GuardResult.deny(
                reason=format_deny(file_path, denies),
                context=format_deny(file_path, denies),
            )

        if infos:
            return GuardResult.allow(context=format_deny(file_path, infos))

        return None
