#!/usr/bin/env python3
"""cc_cortex.butterfly_guard — Butterfly Effect Guard: see it, own it, fix it.

@module butterfly_guard
@responsibility Capture issues from ANY tool result (lint, tsc, test, stderr errors),
    track them in a session-scoped Issue Ledger, and DENY further non-fix operations
    until all discovered issues are resolved or explicitly deferred to handoff.
@dependencies cc_cortex.guards.base, cc_cortex.core.state_store, cc_cortex.core.path_utils
@exports ButterflyGuard, IssueLedger, DiscoveredIssue

I see what others walk past. A crack ignored today is a collapse tomorrow.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Optional

from cc_cortex.constants import WRITE_TOOLS
from cc_cortex.core.log import get_logger
from cc_cortex.core.path_utils import extract_file_path
from cc_cortex.core.state_store import StateStore
from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────

_LEDGER_NS = "butterfly"
_LEDGER_FILE = "issue_ledger.json"

# Tools allowed even with open issues (investigating / verifying)
_ALWAYS_ALLOWED_TOOLS = frozenset({
    "Read", "Grep", "Glob", "WebSearch", "WebFetch",
    "Agent", "TodoWrite",
})

# Tools that indicate the user is fixing something
_FIX_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "Bash"})

# Max issues tracked (prevent unbounded growth)
_MAX_ISSUES = 20

# Max fix attempts before forcing handoff deferral
_MAX_FIX_ATTEMPTS = 3

# ── Error detection patterns ──────────────────────────────

# Patterns that indicate REAL errors in tool output
_ERROR_PATTERNS: list[re.Pattern] = [
    # TypeScript / JavaScript
    re.compile(r"error TS\d+:", re.IGNORECASE),
    re.compile(r"SyntaxError:", re.IGNORECASE),
    re.compile(r"TypeError:", re.IGNORECASE),
    re.compile(r"ReferenceError:", re.IGNORECASE),
    re.compile(r"Cannot find module", re.IGNORECASE),
    re.compile(r"Module not found", re.IGNORECASE),
    # Python
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^\s*(?:ruff|E\d{3,4}|F\d{3,4}|W\d{3,4}).*:\d+:\d+", re.MULTILINE),
    re.compile(r"SyntaxError: invalid syntax"),
    re.compile(r"ImportError:", re.IGNORECASE),
    re.compile(r"ModuleNotFoundError:", re.IGNORECASE),
    # Test failures
    re.compile(r"FAIL\s+[\w./]", re.IGNORECASE),
    re.compile(r"Tests?:\s+\d+\s+failed", re.IGNORECASE),
    re.compile(r"AssertionError:", re.IGNORECASE),
    re.compile(r"✕|✗|×.*(?:fail|error)", re.IGNORECASE),
    re.compile(r"\d+ failing", re.IGNORECASE),
    # Build errors
    re.compile(r"error\[E\d+\]:", re.IGNORECASE),  # Rust
    re.compile(r"Build failed", re.IGNORECASE),
    re.compile(r"Compilation failed", re.IGNORECASE),
    # Generic but high-signal
    re.compile(r"(?:^|\n)\s*error:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"exit code [1-9]\d*", re.IGNORECASE),
    re.compile(r"command failed", re.IGNORECASE),
]

# Patterns to EXCLUDE (common noise, not real issues)
_NOISE_PATTERNS: list[re.Pattern] = [
    re.compile(r"DeprecationWarning", re.IGNORECASE),
    re.compile(r"ExperimentalWarning", re.IGNORECASE),
    re.compile(r"npm warn", re.IGNORECASE),
    re.compile(r"npm WARN", re.IGNORECASE),
    re.compile(r"PendingDeprecationWarning", re.IGNORECASE),
    re.compile(r"FutureWarning", re.IGNORECASE),
    re.compile(r"UserWarning", re.IGNORECASE),
    re.compile(r"InsecureRequestWarning", re.IGNORECASE),
    re.compile(r"ResourceWarning", re.IGNORECASE),
    re.compile(r"punycode.*deprecated", re.IGNORECASE),
    re.compile(r"warning:.*unused", re.IGNORECASE),
    re.compile(r"MaxListenersExceeded", re.IGNORECASE),
    # Git noise
    re.compile(r"warning: LF will be replaced", re.IGNORECASE),
    re.compile(r"hint: ", re.IGNORECASE),
    # Common false positives in file content
    re.compile(r"error_handler|error_message|error_code|on_error|handle_error", re.IGNORECASE),
    re.compile(r"errorBoundary|ErrorComponent|errorPage", re.IGNORECASE),
]

# Commands that are "verification" commands (running these = trying to verify a fix)
_VERIFY_COMMANDS: list[re.Pattern] = [
    re.compile(r"\bruff\b"),
    re.compile(r"\bpytest\b"),
    re.compile(r"\bvitest\b"),
    re.compile(r"\bjest\b"),
    re.compile(r"\btsc\b"),
    re.compile(r"\bnpx?\s+tsc\b"),
    re.compile(r"\beslint\b"),
    re.compile(r"\bcargo\s+(check|test|clippy)\b"),
    re.compile(r"\bgo\s+(vet|test)\b"),
    re.compile(r"\bnpm\s+(test|run\s+lint|run\s+type-check)\b"),
    re.compile(r"\bpnpm\s+(test|lint|type-check)\b"),
]


# ── Data Types ────────────────────────────────────────────


@dataclass
class DiscoveredIssue:
    """A single issue discovered in a tool result."""

    id: str
    """Unique identifier (timestamp-based)."""
    source_tool: str
    """Tool that produced the output containing this issue."""
    file_path: str
    """File associated with the issue (if identifiable), else empty."""
    error_snippet: str
    """First 200 chars of the error text."""
    category: str
    """Issue category: lint | tsc | test | build | runtime."""
    discovered_at: float
    """Unix timestamp when discovered."""
    fix_attempts: int = 0
    """How many times we've tried to fix this."""
    resolved: bool = False
    """True when re-check passes."""
    deferred_reason: str = ""
    """If non-empty, issue was deferred with this reason (written to handoff)."""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> DiscoveredIssue:
        return DiscoveredIssue(
            id=d.get("id", ""),
            source_tool=d.get("source_tool", ""),
            file_path=d.get("file_path", ""),
            error_snippet=d.get("error_snippet", ""),
            category=d.get("category", "unknown"),
            discovered_at=d.get("discovered_at", 0.0),
            fix_attempts=d.get("fix_attempts", 0),
            resolved=d.get("resolved", False),
            deferred_reason=d.get("deferred_reason", ""),
        )


# ── Issue Ledger ──────────────────────────────────────────


class IssueLedger:
    """Session-scoped issue tracker. Persists to state store.

    Core principle: If you see it, you own it. No issue goes unaddressed.
    """

    def __init__(self, cache_dir: str = "", session_id: str = ""):
        self._cache_dir = cache_dir
        self._session_id = session_id
        self._issues: list[DiscoveredIssue] = []
        self._loaded = False

    def _store(self) -> StateStore:
        d = self._cache_dir or os.path.join(
            os.environ.get("CLAUDE_PROJECT_DIR", "."), ".cc_cortex_cache",
        )
        return StateStore(d)

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            data = self._store().read_flat(_LEDGER_NS, _LEDGER_FILE, default={})
            stored_session = data.get("session_id", "")
            # Different session = fresh ledger
            if stored_session and stored_session != self._session_id:
                self._issues = []
            else:
                raw = data.get("issues", [])
                self._issues = [DiscoveredIssue.from_dict(d) for d in raw if isinstance(d, dict)]
        except Exception:
            self._issues = []
        self._loaded = True

    def _save(self) -> None:
        self._store().write_flat(_LEDGER_NS, _LEDGER_FILE, {
            "session_id": self._session_id,
            "issues": [i.to_dict() for i in self._issues],
        })

    def add(self, issue: DiscoveredIssue) -> bool:
        """Add an issue. Returns False if duplicate (same file+category already open)."""
        self._load()
        # Dedup: same file + same category + not resolved
        for existing in self._issues:
            if (
                not existing.resolved
                and not existing.deferred_reason
                and existing.file_path == issue.file_path
                and existing.category == issue.category
            ):
                # Update snippet with fresher error
                existing.error_snippet = issue.error_snippet
                self._save()
                return False
        # Cap
        if len(self._issues) >= _MAX_ISSUES:
            # Remove oldest resolved
            resolved = [i for i in self._issues if i.resolved or i.deferred_reason]
            if resolved:
                self._issues.remove(resolved[0])
            else:
                self._issues.pop(0)
        self._issues.append(issue)
        self._save()
        return True

    def open_issues(self) -> list[DiscoveredIssue]:
        """Return all unresolved, non-deferred issues."""
        self._load()
        return [
            i for i in self._issues
            if not i.resolved and not i.deferred_reason
        ]

    def resolve_for_file(self, file_path: str) -> int:
        """Mark all issues for a file as resolved. Returns count resolved."""
        self._load()
        norm = os.path.normpath(file_path) if file_path else ""
        count = 0
        for issue in self._issues:
            if not issue.resolved and not issue.deferred_reason:
                if issue.file_path and os.path.normpath(issue.file_path) == norm:
                    issue.resolved = True
                    count += 1
        if count:
            self._save()
        return count

    def resolve_by_category(self, category: str) -> int:
        """Mark all issues of a category as resolved."""
        self._load()
        count = 0
        for issue in self._issues:
            if not issue.resolved and not issue.deferred_reason and issue.category == category:
                issue.resolved = True
                count += 1
        if count:
            self._save()
        return count

    def increment_fix_attempt(self, file_path: str) -> None:
        """Increment fix attempt counter for issues on a file."""
        self._load()
        norm = os.path.normpath(file_path) if file_path else ""
        changed = False
        for issue in self._issues:
            if (
                not issue.resolved
                and not issue.deferred_reason
                and issue.file_path
                and os.path.normpath(issue.file_path) == norm
            ):
                issue.fix_attempts += 1
                changed = True
        if changed:
            self._save()

    def get_stuck_issues(self) -> list[DiscoveredIssue]:
        """Return issues that have hit max fix attempts."""
        self._load()
        return [
            i for i in self._issues
            if not i.resolved
            and not i.deferred_reason
            and i.fix_attempts >= _MAX_FIX_ATTEMPTS
        ]

    def defer_issue(self, issue_id: str, reason: str) -> bool:
        """Defer an issue with a reason (must be written to handoff)."""
        self._load()
        for issue in self._issues:
            if issue.id == issue_id:
                issue.deferred_reason = (
                    reason or "Deferred — requires investigation in next session"
                )
                self._save()
                return True
        return False

    def summary(self) -> str:
        """Human-readable summary of open issues."""
        open_issues = self.open_issues()
        if not open_issues:
            return ""
        lines = [f"🦋 {len(open_issues)} discovered issue(s) pending resolution:"]
        for i, issue in enumerate(open_issues, 1):
            loc = os.path.basename(issue.file_path) if issue.file_path else "unknown"
            att = issue.fix_attempts
            attempts = f" (attempt {att}/{_MAX_FIX_ATTEMPTS})" if att else ""
            lines.append(f"  {i}. [{issue.category}] {loc}: {issue.error_snippet[:80]}{attempts}")
        return "\n".join(lines)

    def handoff_block(self) -> str:
        """Generate handoff text for unresolved issues."""
        self._load()
        unresolved = [i for i in self._issues if not i.resolved]
        if not unresolved:
            return ""
        lines = ["### 🦋 Butterfly Effect — Unresolved Issues"]
        for issue in unresolved:
            status = "⏸ deferred" if issue.deferred_reason else "⬜ open"
            loc = issue.file_path or "unknown"
            lines.append(f"- {status} [{issue.category}] `{loc}`: {issue.error_snippet[:120]}")
            if issue.deferred_reason:
                lines.append(f"  Reason: {issue.deferred_reason}")
        return "\n".join(lines)


# ── Detection Logic ───────────────────────────────────────


def _is_noise(text: str) -> bool:
    """Check if text is dominated by noise patterns (false positives)."""
    for pat in _NOISE_PATTERNS:
        if pat.search(text):
            return True
    return False


def _classify_error(text: str) -> str:
    """Classify error text into a category."""
    lower = text.lower()
    if "error ts" in lower or "cannot find module" in lower or "type error" in lower:
        return "tsc"
    if "ruff" in lower or re.search(r"[EWFC]\d{3,4}", text):
        return "lint"
    if any(w in lower for w in ("fail", "assert", "test", "✕", "✗")):
        return "test"
    if any(w in lower for w in ("build failed", "compilation failed", "error[e")):
        return "build"
    return "runtime"


def _extract_file_from_error(text: str) -> str:
    """Try to extract a file path from error text."""
    # Common patterns: "file.ts:10:5", "file.py:42:", "/path/to/file.rs"
    patterns = [
        re.compile(r"([a-zA-Z]:[/\\][\w./\\-]+\.\w+)(?::\d+)?"),  # Windows absolute
        re.compile(r"(/[\w./\\-]+\.\w+)(?::\d+)?"),  # Unix absolute
        re.compile(r"([\w./\\-]+\.(?:ts|tsx|js|jsx|py|rs|go))(?::\d+)?"),  # Relative
    ]
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(1)
    return ""


def _match_error_line(line: str) -> bool:
    """Check if a single line matches any error pattern."""
    for pat in _ERROR_PATTERNS:
        if pat.search(line):
            return True
    return False


def _build_issue(
    tool_name: str, line: str, category: str, file_path: str, index: int,
) -> DiscoveredIssue:
    """Create a DiscoveredIssue from a matched error line."""
    error_file = _extract_file_from_error(line) or file_path
    return DiscoveredIssue(
        id=f"bf-{int(time.time() * 1000)}-{index}",
        source_tool=tool_name,
        file_path=error_file,
        error_snippet=line[:200],
        category=category,
        discovered_at=time.time(),
    )


def detect_issues(
    tool_name: str,
    tool_result: str,
    *,
    file_path: str = "",
) -> list[DiscoveredIssue]:
    """Scan tool output for issues. Returns list of discovered issues.

    Args:
        tool_name: The tool that produced this output.
        tool_result: The stdout/stderr text from the tool.
        file_path: File associated with the tool call (if known).

    Returns:
        List of DiscoveredIssue objects. Empty if no issues found.
    """
    if not tool_result or len(tool_result) < 5:
        return []

    issues: list[DiscoveredIssue] = []
    seen_categories: set[str] = set()

    for line in tool_result.split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) < 5 or _is_noise(stripped):
            continue
        if not _match_error_line(stripped):
            continue
        category = _classify_error(stripped)
        if category in seen_categories:
            continue
        seen_categories.add(category)
        issues.append(_build_issue(tool_name, stripped, category, file_path, len(issues)))

    return issues


def is_verify_command(command: str) -> bool:
    """Check if a Bash command is a verification command (lint/test/tsc)."""
    for pat in _VERIFY_COMMANDS:
        if pat.search(command):
            return True
    return False


def is_clean_result(tool_result: str) -> bool:
    """Check if a tool result indicates all issues are resolved (clean run)."""
    if not tool_result:
        return False
    # Positive signals of clean runs
    clean_patterns = [
        re.compile(r"All checks passed", re.IGNORECASE),
        re.compile(r"0 error", re.IGNORECASE),
        re.compile(r"no (?:issues|errors|problems)", re.IGNORECASE),
        re.compile(r"Tests?:\s+\d+\s+passed,?\s*0\s+failed", re.IGNORECASE),
        re.compile(r"✓.*(?:pass|ok|success)", re.IGNORECASE),
        re.compile(r"All \d+ tests passed", re.IGNORECASE),
        re.compile(r"Found 0 errors", re.IGNORECASE),
    ]
    for pat in clean_patterns:
        if pat.search(tool_result):
            return True
    # No error patterns found at all
    for pat in _ERROR_PATTERNS:
        if pat.search(tool_result):
            return False
    return True


# ── Guard Implementation ──────────────────────────────────


class ButterflyGuard(BaseGuard):
    """Pre+PostToolUse: Butterfly Effect enforcement.

    I see what others walk past. A crack ignored today is a collapse tomorrow.

    PostToolUse: Scan tool results for errors → record in Issue Ledger.
    PreToolUse: Open issues exist → DENY non-fix operations.

    Resolution paths:
      1. Fix the issue → re-run verification → clean result → auto-clear.
      2. Hit max attempts (3) → must write issue to handoff → then continue.
    """

    name = "butterfly_guard"
    category = GuardCategory.QUALITY
    step_back_reason = (
        "Discovered issues must be resolved before moving on. "
        "A builder who walks past a crack in the foundation "
        "is building on borrowed time."
    )

    def __init__(self) -> None:
        self._ledger: Optional[IssueLedger] = None

    def _get_ledger(self, ctx: GuardContext) -> IssueLedger:
        if self._ledger is None or self._ledger._session_id != ctx.session_id:
            self._ledger = IssueLedger(
                cache_dir=ctx.cache_dir,
                session_id=ctx.session_id,
            )
        return self._ledger

    def _check_stuck(
        self, ledger: IssueLedger,
    ) -> Optional[GuardResult]:
        """Deny if issues hit max fix attempts — force handoff deferral."""
        stuck = ledger.get_stuck_issues()
        if not stuck:
            return None
        stuck_files = ", ".join(
            os.path.basename(i.file_path) if i.file_path else "?" for i in stuck[:3]
        )
        return GuardResult.deny(
            reason=(
                f"🦋 {len(stuck)} issue(s) hit {_MAX_FIX_ATTEMPTS} fix attempts "
                f"({stuck_files}). Write them to the handoff file's 'Unresolved' section "
                f"with the error details, then they will be cleared."
            ),
            context=ledger.summary(),
        )

    def _is_fix_action(
        self, ctx: GuardContext, open_issues: list[DiscoveredIssue], ledger: IssueLedger,
    ) -> bool:
        """Return True if the current tool call targets an open issue."""
        if ctx.tool_name not in _FIX_TOOLS:
            return False

        if ctx.tool_name == "Bash":
            command = ctx.tool_input.get("command", "")
            if is_verify_command(command):
                return True
            return any(i.file_path and i.file_path in command for i in open_issues)

        current_file = extract_file_path(ctx.tool_input) or ""
        if not current_file:
            return False
        norm = os.path.normpath(current_file)
        for issue in open_issues:
            if issue.file_path and os.path.normpath(issue.file_path) == norm:
                ledger.increment_fix_attempt(current_file)
                return True
        return False

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PreToolUse: block non-fix operations when open issues exist.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny if open issues exist and tool is not a fix action.
        """
        if ctx.tool_name in _ALWAYS_ALLOWED_TOOLS:
            return None

        ledger = self._get_ledger(ctx)
        open_issues = ledger.open_issues()
        if not open_issues:
            return None

        stuck_deny = self._check_stuck(ledger)
        if stuck_deny:
            return stuck_deny

        if self._is_fix_action(ctx, open_issues, ledger):
            return None

        issue_files = {os.path.basename(i.file_path) for i in open_issues if i.file_path}
        return GuardResult.deny(
            reason=(
                f"🦋 Butterfly Effect: {len(open_issues)} discovered issue(s) pending. "
                f"Fix them first — a crack ignored today is a collapse tomorrow. "
                f"Files: {', '.join(issue_files) or 'see details'}."
            ),
            context=ledger.summary(),
        )

    def _try_resolve_verify(
        self, command: str, file_path: str, ledger: IssueLedger, result: str,
    ) -> bool:
        """Attempt to resolve issues based on a clean verify command. Returns True if resolved."""
        if not (command and is_verify_command(command) and is_clean_result(result)):
            return False
        cmd_lower = command.lower()
        if "ruff" in cmd_lower or "eslint" in cmd_lower:
            ledger.resolve_by_category("lint")
        elif "tsc" in cmd_lower or "type-check" in cmd_lower:
            ledger.resolve_by_category("tsc")
        elif any(t in cmd_lower for t in ("pytest", "vitest", "jest", "cargo test", "go test")):
            ledger.resolve_by_category("test")
        elif file_path:
            ledger.resolve_for_file(file_path)
        return True

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PostToolUse: scan results for issues + check for resolutions.

        Args:
            ctx: Guard context with tool_name, tool_input, tool_result.

        Returns:
            GuardResult.allow with context if new issues found, else None.
        """
        if not ctx.tool_result:
            return None

        ledger = self._get_ledger(ctx)
        file_path = extract_file_path(ctx.tool_input) or ""
        command = ctx.tool_input.get("command", "") if ctx.tool_name == "Bash" else ""

        # Resolution: clean verify command → clear matching issues
        is_resolved = self._try_resolve_verify(
            command, file_path, ledger, ctx.tool_result,
        )
        if ctx.tool_name == "Bash" and is_resolved:
            return None

        # Issue detection
        new_issues = detect_issues(ctx.tool_name, ctx.tool_result, file_path=file_path)
        if not new_issues:
            if file_path and ctx.tool_name in WRITE_TOOLS:
                ledger.resolve_for_file(file_path)
            return None

        for issue in new_issues:
            ledger.add(issue)

        summary = ledger.summary()
        if summary:
            return GuardResult.allow(
                context=(
                    f"🦋 Butterfly Effect: {len(new_issues)} issue(s) detected. "
                    f"Resolve before continuing.\n{summary}"
                ),
            )
        return None

    def on_stop(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Stop: report any unresolved issues for handoff.

        Args:
            ctx: Guard context.

        Returns:
            GuardResult.allow with handoff block as context if unresolved issues exist.
        """
        ledger = self._get_ledger(ctx)
        block = ledger.handoff_block()
        if block:
            return GuardResult.allow(context=block)
        return None
