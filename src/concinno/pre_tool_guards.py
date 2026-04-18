"""concinno.pre_tool_guards — Generic PreToolUse guards.

@module pre_tool_guards
@responsibility Bash background gate, python -c complexity gate, and Read-First
               enforcement (deny Edit/Write on files not yet Read).
@dependencies concinno.constants, concinno.core.log, concinno.core.path_utils,
             concinno.core.state_store, concinno.guards.base
@exports check_bash, gate_python_c, gate_bash_background,
         log_read, gate_read_first, ReadFirstGuard, BashPythonGuard
"""

from __future__ import annotations

import os
import re
from typing import Optional

from concinno.constants import make_deny
from concinno.core.log import get_logger
from concinno.core.path_utils import normalize_path
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

logger = get_logger(__name__)

_NS = "read_log"  # StateStore namespace

# ── BashGuard ──────────────────────────────────────────────────

_LONG_RUNNING_RE = re.compile(
    r"(?:^|\s)(?:"
    r"npm\s+(?:start|run\s+dev|run\s+serve)"
    r"|npx\s+(?:vite|next)\s+dev"
    r"|python\s+-m\s+(?:http\.server|uvicorn|flask)"
    r"|uvicorn\s+"
    r"|flask\s+run"
    r"|gunicorn\s+"
    r"|while\s+(?:true|:|\[\[)"
    r"|tail\s+-[fF]"
    r"|docker\s+compose\s+up(?!\s+.*-d)"
    r"|watch\s+"
    r"|node\s+.*server"
    r")",
    re.IGNORECASE,
)


def check_bash(tool_input: dict) -> list[str]:
    """Check Bash tool input for risky patterns. Returns warning list."""
    warnings: list[str] = []
    cmd = tool_input.get("command", "")
    bg = tool_input.get("run_in_background", False)

    # Long-running command without background
    if not bg and _LONG_RUNNING_RE.search(cmd):
        warnings.append(
            "⚠ [BashGuard] Long-running command detected "
            "(server/watch/while/tail -f). "
            "A navigator who ties the wheel down can't steer when the wind shifts — "
            "run_in_background: true keeps the helm free."
        )

    # Sleep > 30s without background
    sleep_m = re.search(r"sleep\s+(\d+)", cmd)
    if sleep_m and int(sleep_m.group(1)) > 30 and not bg:
        warnings.append(
            "⚠ [BashGuard] sleep > 30s detected. "
            "Waiting in the foreground is standing still when you could be moving — "
            "run_in_background: true."
        )

    return warnings


# ── PythonGuard ────────────────────────────────────────────────


def check_python_c(tool_input: dict) -> Optional[str]:
    """Check for complex python -c commands (>5 lines). Returns warning or None."""
    cmd = tool_input.get("command", "")
    if "python" in cmd and " -c " in cmd and cmd.count("\n") >= 5:
        return (
            "⚠ [PythonGuard] python -c exceeds 5 lines. "
            "A craftsman doesn't carve a sculpture through a keyhole — "
            "a .py script gives the room to work properly."
        )
    return None


def gate_python_c(tool_input: dict) -> Optional[dict]:
    """DENY python -c commands with >5 lines. Returns deny dict or None."""
    cmd = tool_input.get("command", "")
    if "python" in cmd and " -c " in cmd and cmd.count("\n") >= 5:
        return make_deny(
            "PythonGuard Gate: python -c exceeds 5 lines",
            additionalContext=(
                "Complex python -c one-liners (>5 lines) cause syntax and "
                "encoding issues. A craftsman uses the right workspace — "
                "write a .py script file, then run it."
            ),
        )
    return None


# ── SSH Interactive Guard ─────────────────────────────────────

_SSH_INTERACTIVE_RE = re.compile(
    r"(?:^|\s|&&|\|\||;)"
    r"(?:ssh|scp)\s+"
    r"(?!.*(?:paramiko|python|\.py))",  # allow paramiko-based scripts
    re.IGNORECASE,
)


def gate_ssh_interactive(tool_input: dict) -> Optional[dict]:
    """DENY interactive ssh/scp CLI commands. Use paramiko or deploy.py instead."""
    cmd = tool_input.get("command", "")
    if _SSH_INTERACTIVE_RE.search(cmd):
        return make_deny(
            "SSHGuard Gate: interactive ssh/scp command blocked",
            additionalContext=(
                "CLI ssh/scp commands are interactive and can hang indefinitely. "
                "Use paramiko (Python SSH) via deploy.py or a dedicated upload script. "
                "See kb_deploy SKILL for the paramiko pattern."
            ),
        )
    return None


def gate_bash_background(tool_input: dict) -> Optional[dict]:
    """DENY long-running Bash commands without run_in_background. Returns deny dict or None."""
    cmd = tool_input.get("command", "")
    bg = tool_input.get("run_in_background", False)

    if not bg and _LONG_RUNNING_RE.search(cmd):
        return make_deny(
            "BashGuard Gate: long-running command without background",
            additionalContext=(
                "This command (server/watch/while/tail -f) will block the session. "
                "A navigator keeps the helm free — "
                "re-run with run_in_background: true."
            ),
        )
    return None


# ── ToolRedirect (Bash → dedicated tool) ──────────────────────

# Simple patterns where Bash should be replaced by dedicated tools.
# Only match SIMPLE commands — complex pipes/chains are legitimate.
_TOOL_REDIRECTS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"^(?:rg|grep)\s+(?:-[a-zA-Z]*\s+)*['\"]?[\w\.\-\*\\]+['\"]?\s+\S+$"),
        "Grep",
        "Use the Grep tool instead of Bash grep/rg for content search.",
    ),
    (
        re.compile(r"^(?:cat|head|tail)\s+['\"]?[^\|;&]+['\"]?$"),
        "Read",
        "Use the Read tool instead of cat/head/tail for reading files.",
    ),
    (
        re.compile(r"^find\s+[^\|;&]+$"),
        "Glob",
        "Use the Glob tool instead of find for file search.",
    ),
    (
        re.compile(r"^(?:sed|awk)\s+.*['\"]s/[^/]+/[^/]*/.*['\"]?\s+\S+$"),
        "Edit",
        "Use the Edit tool instead of sed/awk for file modifications.",
    ),
    (
        re.compile(r"^(?:echo|printf)\s+.*[>]{1,2}\s+\S+$"),
        "Write",
        "Use the Write tool instead of echo/printf redirection for creating files.",
    ),
]

# Commands with pipes, semicolons, or && are complex — don't redirect
_COMPLEX_CMD_RE = re.compile(r"[|;&]{1,2}")


def gate_tool_redirect(tool_input: dict) -> Optional[dict]:
    """DENY simple Bash commands that should use dedicated tools."""
    cmd = tool_input.get("command", "").strip()
    if not cmd:
        return None
    # Skip complex commands (pipes, chains)
    if _COMPLEX_CMD_RE.search(cmd):
        return None
    for pattern, tool, msg in _TOOL_REDIRECTS:
        if pattern.match(cmd):
            return make_deny(
                f"ToolRedirect: use {tool} instead of Bash",
                additionalContext=msg,
            )
    return None


# ── ReadFirst ──────────────────────────────────────────────────

_READFIRST_SKIP_EXTS = frozenset({
    ".json", ".jsonl", ".env", ".log", ".tmp", ".lock",
})

_READFIRST_SKIP_BASENAMES = frozenset({
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
})


def log_read(file_path: str, cache_dir: str, session_id: str) -> None:
    """Record a Read operation for this session."""
    try:
        store = StateStore(cache_dir)
        state = store.read(_NS, session_id, default={"reads": [], "updated": ""})

        normalized = normalize_path(file_path)
        reads: list = state.get("reads", [])
        if normalized not in reads:
            reads.append(normalized)
            state["reads"] = reads
            from datetime import datetime, timedelta, timezone

            state["updated"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
            store.write(_NS, session_id, state)
    except Exception as exc:
        logger.debug("log_read failed for %s: %s", file_path, exc)


def check_read_first(
    file_path: str,
    cache_dir: str,
    session_id: str,
) -> Optional[str]:
    """Check if file was Read before Edit/Write. Returns warning or None."""
    try:
        # Skip new files
        if not os.path.exists(file_path):
            return None

        # Skip config/lock/log files
        lower = file_path.lower()
        basename = os.path.basename(lower)
        _, ext = os.path.splitext(lower)
        if ext in _READFIRST_SKIP_EXTS or basename in _READFIRST_SKIP_BASENAMES:
            return None

        store = StateStore(cache_dir)
        state = store.read(_NS, session_id, default={"reads": []})
        reads = state.get("reads", [])

        if not reads:
            short_path = file_path.replace("\\", "/")
            return (
                f"⚠ [ReadFirst] {short_path} not yet read this session. "
                f"A cartographer surveys the terrain before redrawing the map."
            )

        normalized = normalize_path(file_path)
        if normalized not in reads:
            short_path = file_path.replace("\\", "/")
            return (
                f"⚠ [ReadFirst] {short_path} not yet read this session. "
                f"A cartographer surveys the terrain before redrawing the map."
            )

        return None
    except Exception:
        return None


# ── ReadFirst Gate (HARD DENY) ────────────────────────────────

# Files that were created (Write) this session — skip deny for these
_CREATED_THIS_SESSION: set[str] = set()


def log_write_create(file_path: str) -> None:
    """Record that a file was created via Write this session (skip deny)."""
    if file_path:
        _CREATED_THIS_SESSION.add(normalize_path(file_path))


def _count_lines(file_path: str, limit: int) -> int:
    """Count lines in a file up to limit. Returns -1 on error."""
    try:
        count = 0
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for _ in f:
                count += 1
                if count >= limit:
                    return count
        return count
    except Exception:
        return -1


def _should_skip_readfirst(file_path: str) -> bool:
    """Check if file should be skipped for ReadFirst enforcement."""
    if not file_path or not os.path.exists(file_path):
        return True
    lower = file_path.lower()
    basename = os.path.basename(lower)
    _, ext = os.path.splitext(lower)
    if ext in _READFIRST_SKIP_EXTS or basename in _READFIRST_SKIP_BASENAMES:
        return True
    return normalize_path(file_path) in _CREATED_THIS_SESSION


def gate_read_first(
    file_path: str,
    cache_dir: str,
    session_id: str,
    *,
    min_lines: int = 50,
) -> Optional[dict]:
    """DENY Edit/Write on existing files not yet Read this session.

    Returns deny dict for PreToolUse hook, or None if allowed.
    Performance: <2ms (stat + read log JSON).
    """
    try:
        if _should_skip_readfirst(file_path):
            return None

        line_count = _count_lines(file_path, min_lines)
        if line_count < min_lines:
            return None

        normalized = normalize_path(file_path)
        store = StateStore(cache_dir)
        state = store.read(_NS, session_id, default={"reads": []})
        if normalized in state.get("reads", []):
            return None

        short_path = os.path.basename(file_path)
        return make_deny(
            f"ReadFirst Gate: {short_path} ({line_count}+ lines) "
            f"not yet Read this session",
            additionalContext=(
                f"{file_path} has {line_count}+ lines you haven't seen. "
                f"Read it first."
            ),
        )
    except Exception:
        return None  # fail-open


# ── BaseGuard adapters ──────────────────────────────────────────


class ReadFirstGuard(BaseGuard):
    """Self-contained Read/Write tracking + DENY on unread files.

    Side effects handled inside check():
      - Read → log_read() (so gate_read_first knows what was read)
      - Write on new file → log_write_create() (exempt from deny)
      - Edit/Write → gate_read_first() → deny or None
    """

    name = "read_first"
    feature_name = "read_first_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "editing unread file"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Track Read operations and deny Edit/Write on unread files.

        Logs Read calls, exempts newly created files, and denies edits
        to existing files (50+ lines) not yet read this session.

        Args:
            ctx: Guard context with tool_name, tool_input, cache_dir, session_id.

        Returns:
            GuardResult.deny if editing an unread file, or None.
        """
        fp = ctx.tool_input.get("file_path") or ctx.tool_input.get("path") or ""
        if not fp or not ctx.cache_dir or not ctx.session_id:
            return None

        # Log reads for tracking
        if ctx.tool_name == "Read":
            log_read(fp, ctx.cache_dir, ctx.session_id)
            return None

        if ctx.tool_name not in ("Edit", "Write"):
            return None

        # Track Write-creates (new files exempt from deny)
        if ctx.tool_name == "Write" and not os.path.exists(fp):
            log_write_create(fp)
            return None

        result = gate_read_first(fp, ctx.cache_dir, ctx.session_id)
        if result is None:
            return None
        return GuardResult.deny(
            result.get("reason", self.name),
            context=result.get("additionalContext", ""),
        )


class ReadBudgetGuard(BaseGuard):
    """Detect aimless browsing: consecutive Reads without any action."""

    name = "read_budget"
    category = GuardCategory.QUALITY
    _consecutive_reads: int = 0
    _threshold: int = 8

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Track consecutive Reads without Edit/Write/Bash between them.

        After threshold consecutive Reads, inject a nudge (not deny).
        Any non-Read tool resets the counter.
        """
        if ctx.tool_name == "Read":
            self._consecutive_reads += 1
            if self._consecutive_reads >= self._threshold:
                count = self._consecutive_reads
                return GuardResult.allow(
                    context=(
                        f"⚠ [ReadBudget] {count} consecutive Reads without "
                        f"any action (Edit/Write/Bash). "
                        f"Are you exploring with a goal, or browsing? "
                        f"Consider acting on what you've read before reading more."
                    ),
                )
            return None
        # Any non-Read tool resets counter
        self._consecutive_reads = 0
        return None


class BashPythonGuard(BaseGuard):
    """DENY long-running Bash without background + complex python -c."""

    name = "bash_python"
    category = GuardCategory.QUALITY
    step_back_reason = "long command — use background execution"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block long-running Bash without background flag and complex python -c.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny for servers/loops without background or python -c >5 lines, or None.
        """
        if ctx.tool_name != "Bash":
            return None
        from concinno.core.config import get_config
        cfg = get_config()
        # Check bash background gate (feature: bash_background_gate).
        if cfg.feature("bash_background_gate", "enabled"):
            result = gate_bash_background(ctx.tool_input)
            if result is not None:
                return GuardResult.deny(
                    result.get("reason", self.name),
                    context=result.get("additionalContext", ""),
                )
        # Check python -c gate (feature: python_c_gate).
        if cfg.feature("python_c_gate", "enabled"):
            result = gate_python_c(ctx.tool_input)
            if result is not None:
                return GuardResult.deny(
                    result.get("reason", self.name),
                    context=result.get("additionalContext", ""),
                )
        # Check SSH interactive gate
        result = gate_ssh_interactive(ctx.tool_input)
        if result is not None:
            return GuardResult.deny(
                result.get("reason", self.name),
                context=result.get("additionalContext", ""),
            )
        # Check tool redirect gate
        result = gate_tool_redirect(ctx.tool_input)
        if result is not None:
            return GuardResult.deny(
                result.get("reason", self.name),
                context=result.get("additionalContext", ""),
            )
        return None
