"""concinno.guards.agent_dispatch_guard — Token-aware agent dispatch strategy.

COGNITIVE guard: inject subagent delegation advice based on current
token usage zone. Long conversations suffer attention decay; subagents
get clean context windows. This guard tells the model WHEN to delegate.

Also injects a lightweight "result quality check" reminder when a
subagent returns (PostToolUse on Agent tool).

2.10.4 additions: scan subagent prompt for unbounded poll patterns
(``until grep``/``until [`` with no ``timeout``/``date +%s`` guard).
Driven by a live incident — 2026-04-21 subagent F burned $1.01 stuck
in ``until grep -q "DONE" log; do sleep 15; done`` when the background
job never wrote the expected marker. Warn (not deny) so the operator
notices before dispatching the subagent.

@module guards/agent_dispatch_guard
@category COGNITIVE
@priority 315
"""

from __future__ import annotations

import json
import os
import re

from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

# Token thresholds (absolute) — aligned with token_zone.py
_GREEN_CAP = 80_000    # <80K: main agent handles everything
_YELLOW_CAP = 150_000  # 80-150K: delegate concrete tasks
# >150K: delegate almost everything, main = commander only

_STRATEGY_GREEN = (
    "Token zone GREEN (<80K) — main agent handles directly. "
    "Only use subagents for genuinely parallel/independent work."
)

_STRATEGY_YELLOW = (
    "Token zone YELLOW (80-150K) — context pressure building. "
    "Delegate concrete implementation tasks to subagents "
    "(they get clean 200K context). Main agent: decisions + coordination only. "
    "Write detailed prompts — subagents can't see this conversation."
)

_STRATEGY_RED = (
    "Token zone RED (>150K) — attention decay active. Full mode: stay in session. "
    "Delegate ALL implementation to subagents. Main agent = commander: "
    "decide what → write detailed prompts → dispatch → verify results. "
    "Do NOT read/edit files directly — your context is unreliable. "
    "Subagent prompt MUST include all context (file paths, requirements, constraints) "
    "because it cannot see this conversation."
)

_RESULT_CHECK = (
    "Subagent returned. Before trusting the result: "
    "1) Did it actually complete the task (not just say it did)? "
    "2) Any files it claimed to create — do they exist? "
    "3) Does the result contradict what you know? "
    "If suspicious, verify with Read/Grep before proceeding."
)

# 2.10.4: unbounded poll pattern detection in subagent prompts.
# These patterns wait for a log keyword without a hard timeout — if the
# background job crashes silently (OOM / SSH drop / unexpected exit) the
# marker never lands and the subagent sleeps forever until an outer
# watchdog stops it. Caller should add a timeout check (``date +%s``
# elapsed cap) or poll for a result file / PID instead.
_POLL_PATTERNS = (
    re.compile(r"until\s+grep\b", re.IGNORECASE),
    re.compile(r"until\s+\[", re.IGNORECASE),
    re.compile(r"while\s+!\s*grep\b", re.IGNORECASE),
)

_TIMEOUT_GUARDS = (
    re.compile(r"\bdate\s+\+%s\b"),
    re.compile(r"\btimeout[=\s]"),
    re.compile(r"\$SECONDS\b"),
    # file/dir-exist tests are valid exit conditions (recommended pattern in
    # feedback_subagent_poll_marker_fragile.md: `until [ -f result.json ]`).
    re.compile(r"\[\s*-[efdse]\b"),
    # PID-liveness check (recommended pattern: `while kill -0 $PID`).
    re.compile(r"\bkill\s+-0\b"),
)

_POLL_WARN = (
    "Subagent brief contains an unbounded poll loop "
    "(`until grep` / `until [` / `while ! grep`) with no timeout guard. "
    "This is the same pattern that burned $1.01 in the 2026-04-21 "
    "subagent F incident — when the background job crashed silently and "
    "never wrote the expected marker, the subagent polled forever until "
    "the outer watchdog killed the pod. Add one of: "
    "(1) hard timeout with `date +%s` elapsed cap, "
    "(2) poll result file `until [ -f /path/to/result.json ]`, "
    "(3) poll PID `while kill -0 $PID 2>/dev/null`. "
    "Escape with `CONCINNO_ALLOW_UNBOUNDED_POLL=1`."
)


def _has_unbounded_poll(prompt: str) -> bool:
    """Return True if ``prompt`` contains a poll loop without timeout guard."""
    if not prompt:
        return False
    if os.environ.get("CONCINNO_ALLOW_UNBOUNDED_POLL") == "1":
        return False
    has_poll = any(p.search(prompt) for p in _POLL_PATTERNS)
    if not has_poll:
        return False
    has_guard = any(g.search(prompt) for g in _TIMEOUT_GUARDS)
    return not has_guard


def _extract_prompt(ctx: GuardContext) -> str:
    """Pull the ``prompt`` parameter out of the Agent tool input."""
    inp = getattr(ctx, "tool_input", None)
    if isinstance(inp, dict):
        val = inp.get("prompt", "")
        return val if isinstance(val, str) else ""
    return ""


def _get_input_tokens() -> int:
    """Read current input token count from zone file."""
    zone_path = os.path.join(
        os.path.expanduser("~"), ".claude", ".token_zone.json",
    )
    try:
        with open(zone_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("input_tokens", 0)
    except Exception:
        return 0


class AgentDispatchGuard(BaseGuard):
    """COGNITIVE: inject token-aware subagent dispatch strategy.

    PreToolUse on Agent tool: advise delegation level.
    PostToolUse on Agent tool: remind to verify result.
    """

    name = "agent_dispatch"
    category = GuardCategory.COGNITIVE
    priority = 315
    hook_event = "PreToolUse"

    def check(self, ctx: GuardContext) -> GuardResult:
        # PreToolUse: inject strategy when spawning agents
        if ctx.hook_event == "PreToolUse" and ctx.tool_name == "Agent":
            tokens = _get_input_tokens()
            if tokens > _YELLOW_CAP:
                base = _STRATEGY_RED
            elif tokens > _GREEN_CAP:
                base = _STRATEGY_YELLOW
            else:
                base = _STRATEGY_GREEN
            # 2.10.4: warn on unbounded poll patterns in subagent brief
            if _has_unbounded_poll(_extract_prompt(ctx)):
                return GuardResult.allow(context=base + "\n\n" + _POLL_WARN)
            return GuardResult.allow(context=base)

        # PostToolUse: result quality check reminder
        if ctx.hook_event == "PostToolUse" and ctx.tool_name == "Agent":
            return GuardResult.allow(context=_RESULT_CHECK)

        return GuardResult.allow()
