"""concinno.agent_gate — Agent spawn gate with counting, escalation, and hard deny.

@module agent_gate
@responsibility Classify agents (research vs execution), count spawns per session,
    escalate warnings at thresholds, and hard-deny when execution cap exceeded.
    Research agents are uncapped. Includes misuse detection.
@dependencies concinno.constants, concinno.guards.base
@exports check, is_research_agent, gate_agent_cap, AgentGateGuard,
    _check_prompt_quality
"""

from __future__ import annotations

import os
import re
from typing import Optional

from concinno.constants import make_allow, make_deny
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ─── Constants ────────────────────────────────────────────

DEFAULT_THRESHOLDS = [3, 4]  # escalation at these counts
DEFAULT_MAX_SPAWNS = 4

# Subagent types that are inherently read-only (no Edit/Write tools)
_RESEARCH_SUBAGENT_TYPES = {"Explore", "Plan", "claude-code-guide"}

# Prompt keywords loaded from i18n locale files
def _research_keywords() -> list[str]:
    from concinno.i18n import patterns
    return patterns("research_keywords")


def _execution_keywords() -> list[str]:
    from concinno.i18n import patterns
    return patterns("execution_keywords")


def _misuse_patterns() -> dict[str, list[str]]:
    from concinno.i18n import patterns
    return {
        "read": patterns("misuse.read"),
        "search": patterns("misuse.search"),
        "edit": patterns("misuse.edit"),
    }

_MISUSE_HINTS = {
    "read": "Use Read tool directly instead of spawning an agent",
    "search": (
        "Simple searches: Grep/Glob. Only use Explore agent "
        "for complex multi-step exploration"
    ),
    "edit": "Edit/Write from main thread, no agent needed",
}

# Code task detection pattern
_CODE_TASK_RE = re.compile(
    r"(?:implement|create|write|build|refactor|fix|module|class|function|"
    r"component|實作|建立|寫|模組|重構|修復|新增)",
    re.IGNORECASE,
)

# Prompt quality: keywords that indicate delivery awareness
_QUALITY_CHECKS: list[tuple[str, re.Pattern[str]]] = [
    ("test requirements", re.compile(
        r"test|測試|vitest|jest|pytest|spec", re.IGNORECASE,
    )),
    ("export/wiring requirements", re.compile(
        r"export|import|匯出|接線|index\.", re.IGNORECASE,
    )),
]


# ─── Internal helpers ─────────────────────────────────────


def _read_count(count_file: str) -> int:
    """Read current spawn count from file. Returns 0 if missing/corrupt."""
    try:
        if os.path.isfile(count_file):
            with open(count_file) as f:
                return int(f.read().strip())
    except Exception:
        pass
    return 0


def _atomic_increment(count_file: str) -> int:
    """Atomically read+increment+write count with file lock.

    Uses OS-level file locking (msvcrt on Windows, fcntl on Unix)
    to prevent race condition when multiple parallel Agent calls
    hit the gate simultaneously.

    Returns the new count after increment.
    """
    try:
        # Ensure file exists
        if not os.path.isfile(count_file):
            with open(count_file, "w") as f:
                f.write("0")

        fd = os.open(count_file, os.O_RDWR)
        try:
            _lock_fd(fd)
            raw = os.read(fd, 64).decode().strip()
            count = int(raw) if raw else 0
            count += 1
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(count).encode())
        finally:
            _unlock_fd(fd)
            os.close(fd)
        return count
    except Exception:
        # Fallback: best-effort without lock
        count = _read_count(count_file) + 1
        try:
            with open(count_file, "w") as f:
                f.write(str(count))
        except Exception:
            pass
        return count


def _lock_fd(fd: int) -> None:
    """Cross-platform exclusive lock on file descriptor."""
    try:
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1024)
    except ImportError:
        import fcntl  # type: ignore[import-not-found]
        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    """Cross-platform unlock on file descriptor."""
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1024)
    except ImportError:
        import fcntl  # type: ignore[import-not-found]
        fcntl.flock(fd, fcntl.LOCK_UN)


def _count_file_path(session_id: str, state_dir: str) -> str:
    """Return path to session's agent count file."""
    return os.path.join(state_dir, f"{session_id[:8]}_agent_count")


def _detect_misuse(tool_input: dict) -> list[str]:
    """Detect misuse patterns in agent prompt. Returns hint list."""
    prompt = (tool_input.get("prompt") or "")[:200].lower()
    subagent_type = tool_input.get("subagent_type", "")
    hints = []
    for category, keywords in _misuse_patterns().items():
        if any(kw in prompt for kw in keywords):
            if category == "search" and subagent_type == "Explore":
                continue  # Explore agent for search is OK
            hints.append(_MISUSE_HINTS[category])
    return hints


def _check_prompt_quality(tool_input: dict) -> list[str]:
    """Check execution agent prompt for delivery awareness.

    Returns list of missing delivery dimensions. If >=2 missing,
    the caller should deny the spawn.
    Only checks code tasks from execution agents.
    """
    if is_research_agent(tool_input):
        return []
    prompt = (tool_input.get("prompt") or "")[:500]
    if not _CODE_TASK_RE.search(prompt):
        return []
    missing = []
    for label, pattern in _QUALITY_CHECKS:
        if not pattern.search(prompt):
            missing.append(label)
    return missing


def is_research_agent(tool_input: dict) -> bool:
    """Classify whether an agent spawn is research-only (no side effects).

    Returns True for:
      - Known read-only subagent types (Explore, Plan, claude-code-guide)
      - General-purpose agents whose prompt only contains research keywords
        and no execution keywords
    """
    subagent_type = tool_input.get("subagent_type", "")
    if subagent_type in _RESEARCH_SUBAGENT_TYPES:
        return True

    # For general-purpose / unspecified type, inspect prompt
    prompt = (tool_input.get("prompt") or "")[:500].lower()
    has_exec = any(kw in prompt for kw in _execution_keywords())
    if has_exec:
        return False
    has_research = any(kw in prompt for kw in _research_keywords())
    return has_research


# ─── Unified Gate Check ──────────────────────────────────


def check(
    tool_name: str,
    tool_input: dict,
    session_id: str,
    state_dir: str,
    *,
    max_spawns: int = DEFAULT_MAX_SPAWNS,
    thresholds: Optional[list[int]] = None,
    lang: str = "en",
) -> Optional[dict]:
    """Unified agent gate: increment counter + escalate + deny at cap.

    Research-type agents (Explore, Plan, read-only prompts) are always
    allowed — only execution-type agents count toward the cap.

    Args:
        tool_name: Must be "Agent" to trigger.
        tool_input: Tool input dict (prompt, subagent_type, etc).
        session_id: Current session ID.
        state_dir: Directory for persisting spawn counts.
        max_spawns: Hard cap for execution-type agents (default 4).
        thresholds: [warn_count, critical_count]. Default [3, 4].
        lang: "en" or "zh" for message language.

    Returns:
        None if not Agent / no session.
        Dict with permissionDecision="deny" if execution-type over cap.
        Dict with permissionDecision="allow" + level/count/hints otherwise.
    """
    if tool_name != "Agent" or not session_id:
        return None

    if thresholds is None:
        thresholds = DEFAULT_THRESHOLDS

    # Research-type agents: always allow, no counting
    if is_research_agent(tool_input):
        return make_allow(
            level="info", count=0, agent_type="research",
            hints=_detect_misuse(tool_input),
        )

    # Prompt quality gate: deny code tasks missing delivery awareness
    missing = _check_prompt_quality(tool_input)
    if len(missing) >= 2:
        return make_deny(
            f"Agent prompt missing: {', '.join(missing)}",
            additionalContext=(
                "Code task agent must mention test + export/wiring. "
                "Add delivery requirements to prompt and retry."
            ),
        )

    # Execution-type: count + cap + escalate
    return _gate_execution(
        tool_input, session_id, state_dir, max_spawns, thresholds,
    )


def _gate_execution(
    tool_input: dict,
    session_id: str,
    state_dir: str,
    max_spawns: int,
    thresholds: list[int],
) -> dict:
    """Count, escalate, and cap execution-type agent spawns."""
    os.makedirs(state_dir, exist_ok=True)
    count_file = _count_file_path(session_id, state_dir)
    count = _atomic_increment(count_file)
    hints = _detect_misuse(tool_input)

    if count > max_spawns:
        return make_deny(
            f"Execution Agent Cap: {count}/{max_spawns} — limit reached",
            additionalContext=(
                f"Exec agent cap {count}/{max_spawns} hit. "
                f"Use direct tools or research agents (uncapped)."
            ),
            level="deny", count=count,
            agent_type="execution", hints=hints,
        )

    warn_at = thresholds[0] if thresholds else 3
    crit_at = thresholds[1] if len(thresholds) > 1 else 4
    if count >= crit_at:
        level = "critical"
    elif count >= warn_at:
        level = "warning"
    else:
        level = "info"

    return make_allow(
        level=level, count=count,
        agent_type="execution", hints=hints,
    )


# ── Backward-compatible read-only gate ───────────────────


def gate_agent_cap(
    tool_name: str,
    session_id: str,
    state_dir: str,
    *,
    max_spawns: int = DEFAULT_MAX_SPAWNS,
) -> Optional[dict]:
    """Read-only deny check (does NOT increment counter).

    Kept for backward compatibility. New code should use ``check()``
    which handles increment + escalation + deny in one call.

    Returns deny dict for PreToolUse hook, or None if allowed.
    Performance: <1ms (read counter file).
    """
    if tool_name != "Agent" or not session_id:
        return None

    count_file = _count_file_path(session_id, state_dir)
    count = _read_count(count_file)

    if count <= max_spawns:
        return None

    return make_deny(
        (
            f"Agent Cap: {count}/{max_spawns} spawns this session — "
            f"limit reached"
        ),
        additionalContext=(
            f"You've spawned {count} agents this session (cap: {max_spawns}). "
            f"Use direct tools (Read, Grep, Glob, Edit) for remaining work. "
            f"Agent spawns waste tokens when direct tools suffice."
        ),
    )


# ── BaseGuard adapter ───────────────────────────────────────────


class AgentGateGuard(BaseGuard):
    """Execution agent spawn cap with research/execution classification."""

    name = "agent_gate"
    feature_name = "agent_cap"
    category = GuardCategory.QUALITY
    step_back_reason = "agent spawn limit exceeded"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block execution-type Agent spawns exceeding the session cap.

        Also denies code-task agents missing delivery awareness
        (test + export/wiring requirements in prompt).

        Returns:
            GuardResult.deny when cap exceeded or prompt quality fails.
        """
        if ctx.tool_name != "Agent" or not ctx.session_id:
            return None
        state_dir = (
            os.path.join(ctx.cache_dir, "agent_gate")
            if ctx.cache_dir else ""
        )
        if not state_dir:
            return None
        result = check(
            ctx.tool_name, ctx.tool_input,
            ctx.session_id, state_dir,
        )
        if result and result.get("permissionDecision") == "deny":
            return GuardResult.deny(
                result.get("reason", self.name),
                context=result.get("additionalContext", ""),
            )
        return None
